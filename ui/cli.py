"""CLI for Coding Agent — Claude Code style UX with prompt_toolkit."""

import os
import sys

# Must be before any other imports - suppress asyncio debug output
os.environ["PYTHONWARNINGS"] = "ignore"
os.environ["PYTHONASYNCIODEBUG"] = "0"
os.environ["AIODEBUG"] = "0"

import asyncio
import time

# Silence pattern for noisy asyncio debug warnings
_NOISE_PATTERNS = ("Executing ", "took ", "Task was destroyed")
from pathlib import Path  # noqa: E402 — kept here for clarity near related setup

# Suppress stderr in non-TTY mode to hide asyncio noise
if not sys.stdin.isatty():
    sys.stderr = open(os.devnull, "w")

# Detect $NO_COLOR
_NO_COLOR = os.environ.get("NO_COLOR", "") != ""


# ── Silence threading-shutdown tracebacks ──────────────────────
# When a confirm prompt is running in a ThreadPoolExecutor and the user
# hits Ctrl+C, the main asyncio loop cancels but the executor thread
# stays blocked on input(). On interpreter exit, Python 3.14's
# concurrent.futures.thread._python_exit() tries to join that thread,
# and a KeyboardInterrupt surfaces as "Exception ignored on threading
# shutdown". The thread is harmless — input() returns EOFError naturally
# when stdin closes — so we just silence the traceback.
def _silent_unraisable(unraisable):
    exc = unraisable.exc_value
    if isinstance(exc, KeyboardInterrupt):
        return
    # Fall through to default for real errors
    sys.__unraisablehook__(unraisable)


sys.unraisablehook = _silent_unraisable


# ANSI color codes (disabled if $NO_COLOR is set)
if _NO_COLOR:
    RESET = BOLD = DIM = YELLOW = GREEN = CYAN = MAGENTA = RED = ""
    CLEAR_LINE = ""
else:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RED = "\033[31m"
    CLEAR_LINE = "\r\033[K"


# Rich console for advanced terminal output (panels, markdown, status)
try:
    from rich.console import Console as _RichConsole

    _RICH = _RichConsole(no_color=_NO_COLOR, highlight=False, soft_wrap=True)
    RICH_AVAILABLE = True
except ImportError:
    _RICH = None
    RICH_AVAILABLE = False


def print_banner():
    """Print banner - Claude Code style."""
    print(f"{CYAN}╭─{RESET}")
    print(f"{CYAN}│{RESET}  {BOLD}{CYAN}Coding Agent{RESET}")
    print(f"{CYAN}╰─{RESET} {DIM}Type a task, /help for commands, Ctrl+C or 'quit' to exit{RESET}")
    print()


# ── M4 P0: plan-approval auto-detect ─────────────────────────────────────
# After LLM outputs a plan in plan mode, user says '是/需要/yes/ok'. The
# LLM doesn't know to call exit_plan_mode and burns 30+ write attempts
# in a loop. This detector catches the approval and auto-accepts the
# plan BEFORE the LLM sees the message.
_PLAN_APPROVAL_WORDS = frozenset(
    {
        "y",
        "yes",
        "ok",
        "okay",
        "do it",
        "go",
        "go ahead",
        "proceed",
        "approve",
        "ship it",
        "lgtm",
        "sure",
        "yep",
        "yup",
        "yeah",
        "confirm",
        "execute",
        "run it",
        "looks good",
        "是",
        "好",
        "可以",
        "需要",
        "对",
        "行",
        "好的",
        "确认",
        "执行",
        "同意",
        "是是",
        "做吧",
        "开始",
        "继续",
        "搞起",
        "干",
        "弄",
        "来",
        "上",
        "好哒",
        "好嘞",
        "okk",
        "ja",
        "oui",
        "si",
    }
)


def _looks_like_plan_approval(user_input: str) -> bool:
    """True if the user's message is a clear plan-approval signal."""
    s = user_input.strip().lower()
    if not s or len(s) > 20:
        return False
    if "?" in s or "？" in s:
        return False
    if s.startswith("/"):
        return False
    if s in _PLAN_APPROVAL_WORDS:
        return True
    first_word = s.split(maxsplit=1)[0]
    if first_word in _PLAN_APPROVAL_WORDS:
        return True
    return False


def cli_has_pending_plan(cli) -> bool:
    """True if the CLI has a plan awaiting user approval."""
    plan = getattr(cli, "_last_plan", None)
    if plan is None:
        return False
    status = getattr(plan, "status", "")
    return status in ("pending", "confirmed", "")


SPINNERS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _render_markdown_token(text: str) -> str:
    """Apply ANSI styles to inline markdown — **bold**, `code`.

    Also filters model-specific artifacts that shouldn't reach the user:
    - <minimax:tool_call>...</minimax:tool_call> blocks (leaked protocol tags)
    - <think>...</think> reasoning blocks
    """
    import re

    if not text:
        return text
    # Filter leaked tool-call tags from models that emit them in content
    text = re.sub(r"<minimax:tool_call>.*?</minimax:tool_call>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    # Inline markdown
    text = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}{YELLOW}\\1{RESET}", text)
    text = re.sub(r"`([^`]+)`", f"{GREEN}\\1{RESET}", text)
    return text


def _render_todo_panel(todos: list) -> str:
    """Render TodoWrite state as a persistent status panel."""
    if not todos:
        return ""
    total = len(todos)
    done = sum(1 for t in todos if t.get("status") == "completed")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")
    pending = total - done - in_progress

    lines = [f"{CYAN}── Todo ─{RESET}".ljust(60) + f"  {DIM}{done}/{total} done{RESET}"]
    for t in todos:
        tid = t.get("id", "?")
        status = t.get("status", "pending")
        content = t.get("content", "")[:50]
        icons = {
            "completed": f"{GREEN}✓{RESET}",
            "in_progress": f"{YELLOW}●{RESET}",
            "pending": f"{DIM}○{RESET}",
        }
        icon = icons.get(status, "?")
        line = f"  {icon} {DIM}[{tid}]{RESET} {content}"
        if status == "in_progress":
            line = f"  {icon} {DIM}[{tid}]{RESET} {BOLD}{content}{RESET}"
        lines.append(line)
    return "\n".join(lines)


_CODE_LANG = ["text"]  # mutable, set by code fence


def _highlight_code_line(line: str) -> str:
    """Apply ANSI color to a code line. Uses Pygments if available."""
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound

        lang = _CODE_LANG[0]
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = get_lexer_by_name("text")
        return highlight(line, lexer, Terminal256Formatter(style="monokai")).rstrip("\n")
    except Exception:
        return f"{DIM}{line}{RESET}"


def _rich_print_markdown(text: str):
    """Render markdown text to terminal using rich — auto-wraps, auto-formats."""
    if not text.strip():
        return
    if RICH_AVAILABLE:
        from rich.markdown import Markdown

        _RICH.print(Markdown(text))
    else:
        print(_render_full_markdown(text))


def _rich_print_todo(todos: list):
    """Render TodoWrite state as a rich Panel."""
    if not todos or not RICH_AVAILABLE:
        return _render_todo_panel(todos)
    from rich.box import ROUNDED
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    total = len(todos)
    done = sum(1 for t in todos if t.get("status") == "completed")
    in_progress = sum(1 for t in todos if t.get("status") == "in_progress")

    table = Table(show_header=False, box=None, padding=(0, 1), expand=False)
    table.add_column(width=3)
    table.add_column()

    icons = {
        "completed": "[green]✓[/green]",
        "in_progress": "[yellow]●[/yellow]",
        "pending": "[dim]○[/dim]",
    }
    for t in todos:
        tid = str(t.get("id", "?"))
        status = t.get("status", "pending")
        content = t.get("content", "")[:60]
        icon = icons.get(status, "?")
        text = Text(content, style="bold" if status == "in_progress" else "")
        table.add_row(icon, f"[dim][{tid}][/dim] {text}")

    title = f"[bold cyan]Todo[/bold cyan]  [dim]{done}/{total} done[/dim]"
    _RICH.print(Panel(table, title=title, border_style="dim", box=ROUNDED))


def _rich_print_confirm(
    tool_name: str, message: str, diff_lines: list, has_diff: bool, choice_prompt: str
):
    """Render the confirm dialog as a rich Panel with Table — Claude Code style."""
    if not RICH_AVAILABLE:
        return False
    from rich.box import ROUNDED
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    # ── Header: tool name + truncated message ──
    header = Text()
    header.append("  ", style="dim")
    header.append(message[:76], style="yellow")

    # ── Diff preview (compact) ──
    body_parts = [header]
    if has_diff:
        diff_text = Text()
        for i, (text, color) in enumerate(diff_lines[:28]):
            if color == "RED":
                diff_text.append(f"  {text}\n", style="red")
            elif color == "GREEN":
                diff_text.append(f"  {text}\n", style="green")
            else:
                diff_text.append(f"  {text}\n", style="dim")
        if len(diff_lines) > 28:
            diff_text.append(
                f"  ... ({len(diff_lines) - 28} more — choose 4 to view all)\n",
                style="dim",
            )
        body_parts.append(diff_text)

    # ── Options table — with explicit keyboard shortcuts ──
    options = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    options.add_column(width=4)
    options.add_column()
    options.add_row(
        "[bold]1[/bold]",
        "[green]Yes[/green]  [dim](y)[/dim]",
    )
    options.add_row(
        "[bold]2[/bold]",
        "[green]Yes, don't ask again[/green]  [dim](a — trust this session)[/dim]",
    )
    options.add_row(
        "[bold]3[/bold]",
        "[red]No[/red]  [dim](n — default; press Enter)[/dim]",
    )
    if has_diff:
        options.add_row(
            "[bold]4[/bold]",
            f"[cyan]View full diff ({len(diff_lines)} lines)[/cyan]  [dim](v)[/dim]",
        )
    body_parts.append(options)

    # ── Footer hint — saves the user when they're stuck ──
    body_parts.append(
        Text(
            "  Tip: type 2 to trust this kind of action for the rest of the session",
            style="dim italic",
        )
    )

    body = Group(*body_parts) if len(body_parts) > 1 else body_parts[0]
    _RICH.print(
        Panel(
            body,
            title=f"[bold cyan]Confirm[/bold cyan]  [dim]{tool_name}[/dim]",
            subtitle="[dim]Enter = default (No)[/dim]",
            border_style="cyan",
            box=ROUNDED,
        )
    )
    return True


def _rich_print_tool_result(name: str, summary: str, success: bool = True):
    """Print a tool result badge — Claude Code style with rich."""
    if RICH_AVAILABLE:
        icon = "✓" if success else "✗"
        color = "green" if success else "red"
        _RICH.print(f"  [{color}]{icon}[/{color}] [dim]{name}[/dim]  {summary}")
    else:
        icon = "✓" if success else "✗"
        color = GREEN if success else RED
        print(f"  {color}{icon}{RESET}  {DIM}{name}{RESET}  {summary}")


def _render_full_markdown(text: str) -> str:
    """Render markdown text to ANSI-styled terminal output.

    Handles headings, lists, code blocks, tables, bold, code, dividers.
    """
    import re

    lines = text.split("\n")
    result = []
    in_code_block = False

    for line in lines:
        # Code block fences
        stripped = line.strip()
        if stripped.startswith("```"):
            # Extract trailing content after the fence (e.g., "```text")
            trailing = stripped[3:].strip()
            in_code_block = not in_code_block
            if in_code_block:
                lang = stripped.lstrip("`").strip() or "text"
                _CODE_LANG[0] = lang
                result.append(f"  {DIM}── {lang} ──{RESET}")
            else:
                result.append(f"  {DIM}─────{RESET}")
            # If there's content after the fence, render it on its own line
            if trailing:
                # Render as text (not in code block — we just toggled)
                styled = _render_markdown_token(trailing)
                result.append(styled)
            continue

        if in_code_block:
            highlighted = _highlight_code_line(line)
            result.append(f"  {DIM}│{RESET} {highlighted}")
            continue

        # Headings
        if line.startswith("### "):
            result.append(f"\n{CYAN}{BOLD}{line[4:]}{RESET}")
            continue
        if line.startswith("## "):
            result.append(f"\n{CYAN}{BOLD}{line[3:]}{RESET}")
            continue

        # Horizontal rule
        if line.strip() == "---":
            result.append(f"{DIM}───{RESET}")
            continue

        # Apply inline styling
        styled = _render_markdown_token(line)

        # Bullet list
        if re.match(r"^\s*[-*]\s", styled):
            result.append(f"  {DIM}•{RESET} {styled[2:]}")
            continue

        # Numbered list
        num_match = re.match(r"^(\s*\d+\.)\s", styled)
        if num_match:
            indent = len(num_match.group(0))
            result.append(f"  {DIM}{num_match.group(1)}{RESET} {styled[indent:]}")
            continue

        # Table rows — align columns
        if "|" in styled and styled.strip().startswith("|"):
            parts = [p.strip() for p in styled.split("|")]
            parts = parts[1:-1] if len(parts) > 2 else parts  # strip leading/trailing |
            if all(p.replace("-", "").replace(":", "").strip() == "" for p in parts if p.strip()):
                # Separator row — skip
                result.append(f"{DIM}{'─' * (len(styled) // 3)}{RESET}")
                continue
            cols = "  ".join(f"{DIM}│{RESET} {p}" for p in parts)
            result.append(f"  {cols}  {DIM}│{RESET}")
            continue

        # Regular line
        result.append(styled)

    return "\n".join(result)


def _build_diff_lines(tool_name: str, args: dict) -> list:
    """Build diff display lines for write/apply_diff tools.

    Returns a list of pre-styled ANSI strings ready to print.
    Each tuple is (text, color) where color is one of the ANSI color names.
    """
    import difflib
    from pathlib import Path as _Path

    lines: list = []
    try:
        if tool_name == "write_file":
            path = args.get("path", "")
            new_content = args.get("content", "")
            if _Path(path).exists():
                old = (
                    _Path(path)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines(keepends=False)
                )
            else:
                old = []
            new = new_content.splitlines(keepends=False)
            full_diff = list(difflib.unified_diff(old, new, lineterm="", n=2))[2:]
            added = sum(1 for l in full_diff if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in full_diff if l.startswith("-") and not l.startswith("---"))
            lines.append((f"+{added} -{removed} {path}", "DIM"))
            for line in full_diff:
                if line.startswith("+++") or line.startswith("---"):
                    continue
                if line.startswith("+"):
                    lines.append((line[:76], "GREEN"))
                elif line.startswith("-"):
                    lines.append((line[:76], "RED"))
                else:
                    lines.append((line[:76], "DIM"))
        elif tool_name == "apply_diff":
            search = args.get("search", "")
            replace = args.get("replace", "")
            if search:
                first_del = search.splitlines()[0] if search else ""
                first_add = replace.splitlines()[0] if replace else ""
                lines.append((f"- {first_del[:60]}", "RED"))
                lines.append((f"+ {first_add[:60]}", "GREEN"))
                if len(search.splitlines()) > 1 or len(replace.splitlines()) > 1:
                    lines.append(
                        (
                            f"  ({len(search.splitlines())} → {len(replace.splitlines())} lines)",
                            "DIM",
                        )
                    )
    except Exception:
        return []
    return lines


async def _confirm_handler(tool_name: str, message: str, args: dict) -> str:
    """Claude Code-style 4-option confirmation prompt with diff preview.

    Options:
      1. Yes (once)
      2. Yes, and don't ask again for this session
      3. No
      4. View full diff (loops: prints the full diff, asks again)

    For write/apply_diff tools, shows a 28-line preview by default.
    Returns "y" / "a" / "n".
    """
    import asyncio as _asyncio

    loop = _asyncio.get_event_loop()
    diff_lines = (
        _build_diff_lines(tool_name, args) if tool_name in ("write_file", "apply_diff") else []
    )
    has_diff = bool(diff_lines)

    PREVIEW_LIMIT = 28  # first N diff lines in the compact view

    while True:
        # ── Rich path: full Panel + Table layout ──
        if RICH_AVAILABLE:
            choice_prompt = (
                f"  {DIM}Choice [1/2/3/4] (default 3 - No):{RESET} "
                if has_diff
                else f"  {DIM}Choice [1/2/3] (default 3 - No):{RESET} "
            )
            # Print a blank line for spacing before the panel
            _RICH.print()
            _rich_print_confirm(tool_name, message, diff_lines, has_diff, choice_prompt)
        else:
            # ── Manual ANSI path ──
            print(f"\n{CYAN}╭─ Confirm ──────────────────────────────{RESET}")
            print(f"{CYAN}│{RESET}  {YELLOW}{message[:76]}{RESET}")
            print(f"{CYAN}│{RESET}")

            if has_diff:
                shown = diff_lines[:PREVIEW_LIMIT]
                for text, color in shown:
                    ansi = {
                        "GREEN": GREEN,
                        "RED": RED,
                        "DIM": DIM,
                        "YELLOW": YELLOW,
                        "CYAN": CYAN,
                    }.get(color, RESET)
                    print(f"{CYAN}│{RESET}  {ansi}{text}{RESET}")
                if len(diff_lines) > PREVIEW_LIMIT:
                    print(
                        f"{CYAN}│{RESET}  {DIM}... ({len(diff_lines) - PREVIEW_LIMIT} more lines — choose 4 to view all){RESET}"
                    )
                print(f"{CYAN}│{RESET}")

            print(f"{CYAN}│{RESET}  {BOLD}1.{RESET} {GREEN}Yes{RESET}  {DIM}(y){RESET}")
            print(
                f"{CYAN}│{RESET}  {BOLD}2.{RESET} {GREEN}Yes, don't ask again{RESET}  {DIM}(a — trust this session){RESET}"
            )
            print(
                f"{CYAN}│{RESET}  {BOLD}3.{RESET} {RED}No{RESET}  {DIM}(n — default; press Enter){RESET}"
            )
            if has_diff:
                print(
                    f"{CYAN}│{RESET}  {BOLD}4.{RESET} {CYAN}View full diff ({len(diff_lines)} lines){RESET}  {DIM}(v){RESET}"
                )
            print(
                f"{CYAN}│{RESET}  {DIM}Tip: type 2 to trust this kind of action for the rest of the session{RESET}"
            )
            print(f"{CYAN}╰────────────────────────────────────────{RESET}")

        try:
            if has_diff:
                prompt = f"  {DIM}Choice [1/2/3/4] (default 3 - No):{RESET} "
            else:
                prompt = f"  {DIM}Choice [1/2/3] (default 3 - No):{RESET} "
            # Use cancellable async input (prompt_toolkit under the hood).
            # Ctrl+C returns "" instead of leaking a thread-pool worker.
            answer = (await _async_input(prompt)).lower()
        except (EOFError, KeyboardInterrupt):
            return "n"

        if not answer:
            # Empty input from Ctrl+C / Ctrl+D — treat as "no"
            return "n"

        if answer in ("1", "y", "yes"):
            return "y"
        if answer in ("2", "a", "always"):
            return "a"
        if answer in ("4", "v", "view", "diff") and has_diff:
            # Print full diff (or paginate if huge)
            print(f"\n{CYAN}── Full diff ({len(diff_lines)} lines) ──{RESET}")
            for i, (text, color) in enumerate(diff_lines, 1):
                ansi = {"GREEN": GREEN, "RED": RED, "DIM": DIM, "YELLOW": YELLOW, "CYAN": CYAN}.get(
                    color, RESET
                )
                print(f"  {DIM}{i:4d}{RESET} {ansi}{text}{RESET}")
                # Page every 50 lines: pause if running interactively
                if i % 50 == 0 and i < len(diff_lines):
                    cont = await _async_input(
                        f"  {DIM}... {len(diff_lines) - i} more — press Enter to continue, q to return: {RESET}"
                    )
                    if cont.lower() in ("q", "quit"):
                        break
            print(f"{CYAN}── End of diff ──{RESET}\n")
            continue  # back to the menu
        # Default + 3 = no
        return "n"


def _clear_two_lines():
    """Clear the 2-line spinner area and return cursor to start of line 1.

    Assumes cursor is at end of line 2 (the metadata line). Steps:
    1. \\r + \\033[K  → clear line 2 (where cursor is)
    2. \\033[1A      → move up to line 1
    3. \\r + \\033[K  → clear line 1
    Result: cursor at column 0 of the original line 1.
    """
    sys.stdout.write("\r\033[K\033[1A\r\033[K")
    sys.stdout.flush()


async def _async_input(prompt: str) -> str:
    """Read a line from stdin asynchronously.

    Uses ``run_in_executor(input, ...)`` — the simplest path that does
    NOT create a second prompt_toolkit ``PromptSession``.  The main CLI
    loop already owns a PromptSession on the terminal; nesting a second
    one inside a confirm dialog causes the two sessions to fight over
    stdin — the user sees the prompt but keyboard input is silently
    swallowed by the outer session.

    Returns:
        Stripped user input. Empty string on Ctrl+D or Ctrl+C.
    """
    try:
        loop = asyncio.get_event_loop()
        return (await loop.run_in_executor(None, lambda: input(prompt))).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


async def _run_with_spinner(coro, label: str = "thinking..."):
    """Run a coroutine while showing a smooth spinner animation.

    The spinner updates independently at 80ms intervals, decoupled from
    the underlying coroutine's progress (LLM API calls, tool execution, etc.).
    """
    done = False
    result = None
    exception = None
    si = 0

    async def _spin():
        nonlocal si
        while not done:
            sys.stdout.write(f"{CLEAR_LINE}{DIM}{SPINNERS[si]} {label}{RESET}")
            sys.stdout.flush()
            si = (si + 1) % len(SPINNERS)
            await asyncio.sleep(0.08)
        # One final clear
        sys.stdout.write(f"{CLEAR_LINE}")
        sys.stdout.flush()

    async def _run():
        nonlocal result, exception, done
        try:
            result = await coro
        except Exception as e:
            exception = e
        finally:
            done = True

    spinner_task = asyncio.create_task(_spin())
    runner_task = asyncio.create_task(_run())
    await runner_task
    await spinner_task  # Wait for final clear

    if exception:
        raise exception
    return result


def _run_async(coro):
    """Run an async coroutine in a new event loop, handling Ctrl+C gracefully.

    Returns the result, or None if cancelled by user.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_debug(False)
    loop.slow_callback_duration = 999  # suppress "took X seconds" warnings

    # Custom exception handler: silence prompt_toolkit's background task warnings
    # that fire on shutdown. These are noise — the code is working correctly.
    def _silent_handler(loop_, context):
        msg = context.get("message", "")
        if any(p in msg for p in _NOISE_PATTERNS):
            return
        # Print remaining (real) exceptions to stderr
        exc = context.get("exception")
        if exc:
            sys.stderr.write(f"Error: {exc}\n")
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_silent_handler)

    try:
        return loop.run_until_complete(coro)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}⏎ Cancelled{RESET}")
        return None
    finally:
        loop.close()


def _tool_icon(name: str, args: dict) -> tuple:
    """Return (icon, label) for a tool call — Claude Code style.

    Prefers tool.render_call() when available (self-describing rendering).
    Falls back to built-in icon map for backward compatibility.
    """
    from agent.tools.base import registry

    path = args.get("path", "")
    cmd = args.get("command", "")[:60]
    query = args.get("query", "")[:60]
    url = args.get("url", "")[:60]
    pkg = args.get("package", "")
    pattern = args.get("pattern", "")[:50]

    # ── Tool-specific rendering (preferred) ───────────────────
    tool = registry.get(name)
    if tool:
        badge = tool.user_facing_name or name
        display = tool.render_call(args)
        default_display = (
            f"{name}: {next(iter(args))}={str(args.get(next(iter(args)), ''))[:40]}"
            if args
            else name
        )
        if display != default_display and display != name:
            return (f"  {DIM}{badge}{RESET}", f"{display}")
        # Use render_call for the label
        return (f"  {DIM}{badge}{RESET}", f"{display}")

    # ── Built-in icons (backward compatible) ──────────────────
    icons = {
        "read_file": (f"{CYAN}@", f"{path}"),
        "write_file": (f"{GREEN}+", f"{BOLD}{path}{RESET}"),
        "apply_diff": (f"{YELLOW}~", f"{path}"),
        "edit_file": (f"{YELLOW}~", f"{path}"),
        "execute_command": (f"{MAGENTA}>", f"{cmd}"),
        "run_tests": (f"{CYAN}◷", f"{DIM}Running tests{RESET}"),
        "web_search": (f"{CYAN}🔍", f"{DIM}{query}{RESET}"),
        "web_fetch": (f"{CYAN}🌐", f"{DIM}{url}{RESET}"),
        "install_package": (f"{GREEN}📦", f"{BOLD}{pkg}{RESET}"),
        "grep": (f"{CYAN}⌕", f"{DIM}{pattern}{RESET}"),
        "code_search": (f"{CYAN}⌕", f"{DIM}{query}{RESET}"),
        "git": (f"{MAGENTA}⑂", f"{DIM}{args.get('command', '')[:50]}{RESET}"),
        "smart_commit": (f"{MAGENTA}⑂", "commit"),
        "create_pr": (f"{MAGENTA}⑂", "create PR"),
        "smart_branch": (f"{MAGENTA}⑂", f"branch: {args.get('task_description', '')[:40]}"),
        "safe_rename": (f"{YELLOW}↻", f"{args.get('symbol', '')} → {args.get('new_name', '')}"),
        "sub_agent": (f"{CYAN}◆", f"{DIM}{args.get('task', '')[:50]}{RESET}"),
        "sandbox_execute": (f"{MAGENTA}▣", f"{cmd}"),
        "delete_file": (f"{RED}×", f"{path}"),
    }

    if name in icons:
        return icons[name]

    # Generic fallback
    key = next(iter(args)) if args else ""
    val = str(args.get(key, ""))[:40]
    return (f"{DIM}→{RESET} {CYAN}{name}{RESET}", f"{DIM}{key}={val}{RESET}")


def _is_simple_question(task: str) -> bool:
    """Detect if the task is a simple conversational question.

    Uses structural signals rather than specific keyword lists:
    - Question markers (? ? ? ?) → likely conversational
    - Action verbs (install, build, fix, ...) → NOT simple
    - Self-referential + question words → likely conversational

    This is a lightweight pre-filter. The intent classifier (LLM-based) is
    the primary routing mechanism — this just catches obvious cases fast.
    """
    t = task.strip()
    t_lower = t.lower()

    # ── Reject: action requests ──────────────────────────────
    # If the user asks to DO something, it's not a simple question.
    # Check BEFORE length — short action requests ("修bug") still need tools.
    action_keywords = (
        "install",
        "uninstall",
        "remove",
        "delete",
        "create",
        "build",
        "write",
        "run",
        "execute",
        "test",
        "fix",
        "refactor",
        "deploy",
        "commit",
        "add",
        "change",
        "update",
        "upgrade",
        "generate",
        "implement",
        "setup",
        "configure",
        "migrate",
        "convert",
        # Chinese action verbs
        "安装",
        "卸载",
        "删除",
        "创建",
        "构建",
        "写",
        "运行",
        "执行",
        "测试",
        "修复",
        "重构",
        "部署",
        "提交",
        "生成",
        "实现",
        "改",
        "加",
        "建",
        "配",
        "配置",
        "迁移",
        "转换",
        "修",
        "装",
        "删",
        "搬",  # single-char action verbs
    )
    if any(k in t_lower for k in action_keywords):
        return False

    # ── Accept: very short ───────────────────────────────────
    if len(t) <= 8:
        return True

    # ── Accept: greetings ────────────────────────────────────
    greetings = (
        "hello",
        "hi ",
        "hey",
        "good morning",
        "good afternoon",
        "what's up",
        "howdy",
        "你好",
        "嗨",
        "早上好",
        "晚上好",
    )
    if any(t_lower.startswith(g) for g in greetings):
        return True

    # ── Accept: question markers (Chinese/English) ────────────
    if t.endswith(("?", "?", "？", "吗", "呢", "吧", "啊")) and len(t) <= 30:
        return True

    # ── Accept: self-referential + question-like ─────────────
    # "你" / "your" → talking about or to the agent
    has_self_ref = any(w in t for w in ("你", "your ", "you ", "you?", "you."))
    # Question-indicating words
    has_question_word = any(
        w in t
        for w in (
            "什么",
            "怎么",
            "为什么",
            "如何",
            "谁",
            "哪",
            "多少",
            "有多",
            "what",
            "how",
            "why",
            "who",
            "where",
            "when",
            "which",
            "吗",
            "呢",
            "吧",
            "?",
            "？",
            "是不是",
            "能不能",
        )
    )
    if has_self_ref and has_question_word:
        return True

    # ── Accept: pure question structure (even without self-ref) ──
    # e.g. "python怎么学", "what is asyncio"
    # Only if short enough and has question words
    if has_question_word and len(t) <= 25:
        return True

    return False


class SimpleCLI:
    """Interactive CLI with slash command support."""

    def __init__(self):
        from .spinner import SpinnerController

        self.history = []
        self._input_history = []
        self.file_context = []
        self._last_engine = None
        self._last_plan = None
        self._last_plan_persistence_path = None  # M1 P0: tracked so /plan edit can rewrite
        self._router = None
        self._should_quit = False
        self._last_todo = None  # Last TodoWrite state for persistent panel
        # Shared spinner — one instance for the whole CLI session
        self._spin = SpinnerController()
        # Session-level token accumulator (reset on each CLI invocation)
        self._session_tokens_in = 0
        self._session_tokens_out = 0
        self._session_tokens_estimated_turns = 0  # how many turns were estimated

    def _echo_user_input(self, text: str) -> None:
        """Synchronously echo the user's submitted input — Claude Code style.

        Mirrors TUI's `chat.mount(RichChatMessage("user", user_input))` at
        `ui/tui.py:293`. Printed BEFORE the first `await` in the run loop,
        so the user sees their bubble the instant they hit Enter — no
        gap, no spinner-first-then-text.

        Rich path: rounded Panel in dim cyan.
        ANSI fallback: `╭─ > you ──╮` three-line box, color codes emptied
        by `NO_COLOR=1`.
        """
        if not text:
            return
        if RICH_AVAILABLE:
            try:
                from rich.box import ROUNDED
                from rich.panel import Panel
                from rich.text import Text as _Text

                # Truncate giant paste previews; full text is in self.history
                MAX_PREVIEW = 2000
                if len(text) > MAX_PREVIEW:
                    preview = text[:MAX_PREVIEW] + f"\n  ... ({len(text) - MAX_PREVIEW} more chars)"
                else:
                    preview = text
                body = _Text(preview, style="cyan")
                _RICH.print(
                    Panel(
                        body,
                        title="[bold cyan]> you[/bold cyan]",
                        title_align="left",
                        border_style="dim cyan",
                        box=ROUNDED,
                        padding=(0, 1),
                    )
                )
                return
            except Exception:
                # Rich path failed for some reason — fall through to ANSI
                pass
        # ANSI fallback (or Rich failure)
        first_line = text.split("\n", 1)[0]
        if "\n" not in text and len(first_line) <= 80:
            print(f"{CYAN}╭─ > you ─────────────────────────────{RESET}")
            print(f"{CYAN}│{RESET} {first_line}")
            print(f"{CYAN}╰─────────────────────────────────────{RESET}")
        else:
            print(f"{CYAN}╭─ > you ─────────────────────────────{RESET}")
            for line in text.split("\n"):
                # Truncate very long lines so the box stays readable
                print(f"{CYAN}│{RESET} {line[:120]}")
            print(f"{CYAN}╰─────────────────────────────────────{RESET}")

    def _build_toolbar(self):
        """Render the always-visible status line below the input.

        `prompt_toolkit` calls this callable on every render (whenever
        the input area refreshes). Returns `FormattedText` — a list of
        (style, text) tuples. Layout: `model · mode` on the left,
        `⬇ in / out tok [(估计)]` on the right.

        Reads from `self._session_tokens_*` (no synchronization needed —
        asyncio is single-threaded; reads are atomic ints).
        """
        try:
            from prompt_toolkit.formatted_text import FormattedText
        except ImportError:
            return ""

        # Pull current model/mode from config (set at startup)
        try:
            from agent.core.config import config as _cfg

            model = _cfg.get("model", "?")
            mode = _cfg.get("mode", "default")
        except Exception:
            model, mode = "?", "default"

        # Strip provider prefix for compactness: "moonshot-v1-8k" not "moonshot/moonshot-v1-8k"
        if "/" in model:
            model = model.split("/", 1)[1]
        # Truncate long model names so the toolbar stays single-line
        if len(model) > 24:
            model = model[:22] + "…"

        left = f"  {model}  ·  {mode}"

        # Right side: session tokens + estimated marker
        in_t = self._session_tokens_in
        out_t = self._session_tokens_out
        est = self._session_tokens_estimated_turns
        if in_t or out_t:
            right = f"⬇ {in_t:,} in / {out_t:,} out"
            if est:
                right += f"  ({est} 估计)"
        else:
            right = ""

        # Pad between left and right; degrade gracefully on narrow terminals
        try:
            import shutil

            width = shutil.get_terminal_size((100, 20)).columns
        except Exception:
            width = 100
        if width < 60:
            # Too narrow for two sides — show model only
            line = f"  {model}"
        else:
            pad = max(1, width - len(left) - len(right) - 2)
            line = left + (" " * pad) + right

        style = "class:toolbar"
        return FormattedText([(style, line)])

    def _read_line(self) -> str:
        """Read a line with prompt_toolkit — Claude Code style.

        Falls back to input() if stdin is not a TTY or prompt_toolkit is unavailable.
        Ctrl+C returns empty string (cancel current input). Ctrl+D on empty returns 'quit'.
        """
        if not sys.stdin.isatty():
            return sys.stdin.readline().strip()

        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.styles import Style
        except ImportError:
            return input(f"{DIM}> {RESET}").strip()

        # Build session lazily (once)
        if not hasattr(self, "_pt_session"):
            bindings = KeyBindings()

            @bindings.add("c-c")
            def _(event):
                """Ctrl+C on empty line: quit. On non-empty: clear line."""
                buf = event.app.current_buffer
                if not buf.text:
                    event.app.exit(result="quit")
                else:
                    buf.reset()

            @bindings.add("c-d")
            def _(event):
                """Ctrl+D on empty line: quit."""
                buf = event.app.current_buffer
                if not buf.text:
                    event.app.exit(result="quit")
                else:
                    buf.delete()

            # Slash command completer
            slash_commands = [
                "/help",
                "/clear",
                "/plan",
                "/commit",
                "/model",
                "/mode",
                "/memory",
                "/status",
                "/context",
                "/review",
                "/undo",
                "/quit",
            ]
            completer = WordCompleter(slash_commands, ignore_case=True, sentence=True)

            # History
            history = InMemoryHistory()

            # Style: minimal — just dim prompt. Add a toolbar class for
            # the bottom_toolbar status line; skip the bg color when
            # $NO_COLOR is set (we already degraded to no color elsewhere).
            style_dict = {"prompt": "dim"}
            if not _NO_COLOR:
                style_dict["toolbar"] = "bg:#1a1a1a #888888"
            style = Style.from_dict(style_dict)

            self._pt_session = PromptSession(
                history=history,
                key_bindings=bindings,
                completer=completer,
                style=style,
                message=[("class:prompt", "> ")],
                multiline=False,
                bottom_toolbar=self._build_toolbar,
            )

        try:
            text = self._pt_session.prompt()
        except (EOFError, KeyboardInterrupt):
            return ""
        except Exception:
            return input(f"{DIM}> {RESET}").strip()

        return text.strip() if text else text

    def _inject_file_context(self, task: str) -> str:
        if not self.file_context:
            return task
        recent = list(dict.fromkeys(self.file_context[-3:]))
        return f"[Files: {', '.join(recent)}] {task}"

    def _update_file_context(self, result: str):
        import re

        paths = re.findall(r"[\w./\-]+\.(?:py|js|ts|go|rs|java|yaml|json|md)", result)
        self.file_context.extend(paths)
        if len(self.file_context) > 10:
            self.file_context = self.file_context[-10:]

    async def _handle_command(self, user_input: str) -> str:
        """Handle a slash command. Returns output string."""
        from agent.commands.base import registry
        from agent.core.config import config

        cmd = registry.matches(user_input)
        if not cmd:
            return f"{RED}Unknown command. Type /help for available commands.{RESET}"

        # Extract args (everything after the command name)
        stripped = user_input[1:]
        parts = stripped.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""

        # Build context for command handlers
        ctx = {
            "cli": self,
            "engine": self._last_engine,
            "model": config.get("model"),
            "provider": config.get("provider"),
            "mode": config.get("mode"),
            "workspace": str(Path.cwd()),
        }

        # Prefer the dataclass field; fall back to _handler (set externally
        # by builtin.py for commands registered without handler=).
        handler = getattr(cmd, "handler", None) or getattr(cmd, "_handler", None)
        if handler:
            try:
                return await handler(args, ctx)
            except Exception as e:
                return f"{RED}Command error: {e}{RESET}"
        else:
            return f"{DIM}Command '{cmd.name}' has no handler.{RESET}"

    def run(self):
        print_banner()
        from agent.core.config import config

        mode = config.get("mode")
        provider = config.get("provider")
        model = config.get("model")
        print(
            f"  {DIM}Model:{RESET} {CYAN}{model}{RESET}  {DIM}Mode:{RESET} {CYAN}{mode}{RESET}  {DIM}Provider:{RESET} {CYAN}{provider}{RESET}"
        )

        # Show project context status
        from pathlib import Path

        context_file = Path.cwd() / "CODING_AGENT.md"
        if context_file.exists():
            size = len(context_file.read_text(encoding="utf-8"))
            print(f"  {DIM}Context:{RESET} {GREEN}CODING_AGENT.md{RESET} ({size} bytes)")

        print(f"  {DIM}Type /help for commands{RESET}")
        print()

        # PR-14: Fire ON_SESSION_START once per CLI session.
        # The default handler loads the user profile (if any) and surfaces
        # it to subsequent turns via the hook payload.
        try:
            import asyncio as _asyncio

            from agent.core.hooks import ON_SESSION_START, HookRegistry
            from agent.core.hooks_session import load_user_profile_on_start

            session_hooks = HookRegistry()
            session_hooks.register(ON_SESSION_START, load_user_profile_on_start)
            payload = {"session_id": f"cli-{id(self)}", "task": None}
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    # In an async context — schedule it
                    _asyncio.ensure_future(session_hooks.execute(ON_SESSION_START, payload))
                else:
                    loop.run_until_complete(session_hooks.execute(ON_SESSION_START, payload))
            except RuntimeError:
                # No event loop — create one
                _asyncio.run(session_hooks.execute(ON_SESSION_START, payload))
        except Exception:
            # Never let session-start failure break startup
            pass

        # ── Set up intent router ──
        try:
            self._setup_router(model, provider)
        except Exception as e:
            print(f"{DIM}Warning: intent router unavailable ({e}){RESET}")
            print()
        route = self._router.route if self._router else None

        while True:
            try:
                user_input = self._read_line()
                if not user_input:
                    # Empty input (Ctrl+C or empty Enter) — just re-prompt
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    self._print_session_total()
                    print(f"{DIM}Goodbye.{RESET}")
                    break
                if self._should_quit:
                    self._print_session_total()
                    print(f"{DIM}Goodbye.{RESET}")
                    break

                # ── M4 P0: auto-approve plan on user "yes" ──
                if (
                    self.engine
                    and getattr(self.engine.permissions, "mode", None)
                    and self.engine.permissions.mode.value == "plan"
                    and cli_has_pending_plan(self)
                    and _looks_like_plan_approval(user_input)
                ):
                    result = _run_async(self._handle_command("/plan accept"))
                    print(f"{GREEN}✓ Auto-accepted plan (user said: {user_input!r}){RESET}")
                    print(result)
                    print()
                    if self._should_quit:
                        self._print_session_total()
                        print(f"{DIM}Goodbye.{RESET}")
                        break
                    continue

                # ── Slash command routing ──
                if user_input.startswith("/"):
                    result = _run_async(self._handle_command(user_input))
                    print(result)
                    print()
                    if self._should_quit:
                        self._print_session_total()
                        print(f"{DIM}Goodbye.{RESET}")
                        break
                    continue

                self.history.append({"role": "user", "content": user_input})
                self._input_history.append(user_input)

                # ── Layer 1: echo user input immediately, before any await
                # ── — gives instant visual feedback that Enter was received
                self._echo_user_input(user_input)

                # ── Intent-based routing (LLM classifier → handler) ──
                task_with_context = self._inject_file_context(user_input)
                result = _run_async(
                    route(task_with_context) if route else self._run_task(task_with_context)
                )
                if result is None:
                    continue

                self.history.append({"role": "assistant", "content": result})
                self._update_file_context(result)
                print()

            except KeyboardInterrupt:
                # Ctrl+C during readline or idle → exit
                self._print_session_total()
                print(f"\n{DIM}Goodbye.{RESET}")
                break
            except EOFError:
                self._print_session_total()
                print(f"\n{DIM}Goodbye.{RESET}")
                break

    def _print_session_total(self) -> None:
        """Print total token usage for the current CLI session.

        Called on /quit, Ctrl+C, or EOF. Shows real values when all turns
        reported usage, plus the count of estimated turns so the user
        knows the precision.
        """
        if self._session_tokens_in == 0 and self._session_tokens_out == 0:
            return
        est_note = ""
        if self._session_tokens_estimated_turns > 0:
            est_note = f"  {DIM}(其中 {self._session_tokens_estimated_turns} 轮为估计值){RESET}"
        print(
            f"\n{DIM}本次会话共消耗: "
            f"in={self._session_tokens_in:,}  out={self._session_tokens_out:,} tokens"
            f"{est_note}{RESET}"
        )

    async def _direct_answer(self, task: str, engine, start_time: float) -> str:
        """Answer a simple conversational question directly, no plan phase.

        Bypasses the coding-agent system prompt and tool registry to get a
        direct LLM response — no tool calls, no plan, just an answer.
        """
        from agent.llm.client import Message

        from .spinner import StageLabel

        # Spinner runs until first token or completion
        self._spin.start(StageLabel.THINKING)

        print()  # blank line for spacing
        buffer = []
        _usage_info = None  # captured from last streaming chunk
        _turn_estimated = False
        try:
            if engine.llm is None:
                await self._spin.stop_async()
                print(f"{RED}No LLM configured{RESET}")
                return ""

            # ── Identity: run fact_extractor on the user input + inject
            # ── user_profile into the prompt. Without this, short messages
            # ── like "我是hay" would never be remembered, and the LLM
            # ── wouldn't see the user's stored identity either.
            if engine.user_profile is not None:
                try:
                    from agent.core.fact_extractor import FactExtractor

                    extractor = FactExtractor(llm_client=engine.llm)
                    extractor.extract_and_apply(task, engine.user_profile)
                except Exception:
                    # Never let identity tracking break a simple answer.
                    pass

            # Brief identity + user question, no tools.
            # Include conversation history so the agent remembers context
            prompt_parts = [
                "You are Coding Agent, an AI programming assistant. "
                "You help users with software engineering tasks — writing code, "
                "fixing bugs, refactoring, and answering technical questions. "
                "You run inside a terminal CLI.\n\n"
                "FORMATTING RULES:\n"
                "- Use **bold** for emphasis (renders as ANSI bold in terminal)\n"
                "- Use `code` for inline code and ```blocks``` for code blocks\n"
                "- Use ## headings for sections\n"
                "- Use bullet lists (- item) and numbered lists (1. item)\n"
                "- For tables, use simple aligned text, NOT ASCII box-drawing chars\n"
                "  (no ┌─┐│└┘├┤). Example:\n"
                "  | Col A | Col B |\n"
                "  |-------|-------|\n"
                "  | val1  | val2  |\n"
                "- Do NOT draw ASCII art boxes — the terminal can't render them\n"
                "- Keep answers concise.",
            ]

            # Inject user profile (persistent identity). If we've met this
            # user before, the LLM sees their name/preferences and can
            # address them properly instead of "sir" / "用户".
            if engine.user_profile is not None:
                profile_xml = engine.user_profile.to_prompt()
                if profile_xml:
                    prompt_parts.append(
                        f"\n{profile_xml}\n"
                        f"Address the user by their name when natural. "
                        f"Use their stated language and preferences."
                    )

            # Inject recent conversation history
            if len(self.history) >= 2:
                recent = self.history[-6:]  # last 3 exchanges
                history_text = "\n".join(f"{h['role']}: {h['content'][:200]}" for h in recent)
                prompt_parts.append(f"\n[Previous conversation]\n{history_text}")

            prompt_parts.append(f"\n\nUser: {task}")
            messages = [Message(role="user", content="\n".join(prompt_parts))]
            response, is_stream = await engine.llm.chat(messages, stream=True)

            if is_stream:
                # Stream with line-buffered markdown rendering.
                # Each line is fully rendered when its trailing \n arrives, so
                # headings/bullets/tables format live. Partial lines get inline
                # only (bold, code) since headings aren't complete yet.
                #
                # Layer 3: pre-first-token, we use rich.live.Live to render
                # the spinner smoothly. On the first content token we exit
                # Live and continue with the existing print loop (which keeps
                # the markdown line-buffering correct).
                _in_think = False
                _first_token = True
                _line_buf = []  # accumulates tokens for the current line
                _at_line_start = True  # for eating leading whitespace

                def _flush_line():
                    """Render and print the current line buffer."""
                    nonlocal _line_buf
                    if not _line_buf:
                        return
                    line_text = "".join(_line_buf)
                    _line_buf = []
                    rendered = _render_full_markdown(line_text)
                    sys.stdout.write(rendered)
                    sys.stdout.flush()
                    buffer.append(line_text)

                # Open Live for the spinner phase; closes on first content
                # token so the markdown print loop can take over with its
                # line-buffered rendering.
                _live_ctx = None
                if RICH_AVAILABLE and sys.stdout.isatty():
                    try:
                        from rich.live import Live
                        from rich.spinner import Spinner as _RichSpinner

                        _live_render = _RichSpinner("dots", text=StageLabel.THINKING, style="dim")
                        _live_ctx = Live(
                            _live_render,
                            console=_RICH,
                            refresh_per_second=12,
                            transient=True,
                        )
                        _live_ctx.__enter__()
                    except Exception:
                        _live_ctx = None
                else:
                    # Non-TTY or no Rich — fall back to SpinnerController
                    self._spin.start(StageLabel.THINKING)

                try:
                    for chunk in response:
                        if hasattr(chunk, "usage") and chunk.usage:
                            _usage_info = {
                                "input": getattr(chunk.usage, "input_tokens", 0)
                                or getattr(chunk.usage, "prompt_tokens", 0),
                                "output": getattr(chunk.usage, "output_tokens", 0)
                                or getattr(chunk.usage, "completion_tokens", 0),
                            }
                        if hasattr(chunk, "choices") and chunk.choices:
                            delta = chunk.choices[0].delta
                            if hasattr(delta, "content") and delta.content:
                                raw = delta.content
                                if "<think>" in raw:
                                    _in_think = True
                                    raw = raw.split("<think>")[0]
                                if _in_think and "</think>" in raw:
                                    _in_think = False
                                    raw = raw.split("</think>")[-1]
                                elif _in_think:
                                    raw = ""
                                if raw:
                                    if _first_token:
                                        # Close Live (transient → spinner
                                        # line vanishes) and stop fallback
                                        # SpinnerController if any.
                                        if _live_ctx is not None:
                                            try:
                                                _live_ctx.__exit__(None, None, None)
                                            except Exception:
                                                pass
                                            _live_ctx = None
                                        else:
                                            await self._spin.stop_async()
                                        sys.stdout.write(f"{GREEN}●{RESET} ")
                                        sys.stdout.flush()
                                        _first_token = False

                                    # Eat leading whitespace at start (including newlines)
                                    if _at_line_start:
                                        leading = raw.lstrip(" \t\r\n")
                                        if not leading:
                                            # pure whitespace token — drop it
                                            continue
                                        consumed = len(raw) - len(leading)
                                        raw = leading
                                        _at_line_start = False

                                    # Split on \n, flush complete lines
                                    parts = raw.split("\n")
                                    for i, part in enumerate(parts):
                                        if i > 0:
                                            _flush_line()
                                            _at_line_start = True
                                            sys.stdout.write("\n")
                                            sys.stdout.flush()
                                        if part:
                                            _line_buf.append(part)
                finally:
                    # Make sure Live is closed even on exception
                    if _live_ctx is not None:
                        try:
                            _live_ctx.__exit__(None, None, None)
                        except Exception:
                            pass
                        _live_ctx = None
            else:
                await self._spin.stop_async()
                content = response if isinstance(response, str) else ""
                if RICH_AVAILABLE and content.strip():
                    _rich_print_markdown(content)
                else:
                    print(_render_full_markdown(content), flush=True)
                buffer.append(content)
        except Exception as e:
            await self._spin.stop_async()
            print(f"{RED}Error: {e}{RESET}", flush=True)
        finally:
            await self._spin.stop_async()
            await engine.shutdown()

        result = "".join(buffer)
        elapsed = time.time() - start_time
        # Session token accumulator
        if _usage_info:
            self._session_tokens_in += _usage_info.get("input", 0)
            self._session_tokens_out += _usage_info.get("output", 0)
        else:
            _turn_estimated = True
            # Best-effort local estimate for session tally
            est = max(1, len(result) // 4)
            self._session_tokens_out += est
        if _turn_estimated:
            self._session_tokens_estimated_turns += 1
        # Footer — Claude Code style with real vs estimated label
        if _usage_info:
            inp = _usage_info.get("input", 0)
            out = _usage_info.get("output", 0)
            window = 128000
            pct = inp / window * 100 if window > 0 else 0
            remaining = max(0, 100 - pct)
            if remaining < 25:
                line = (
                    f"Context low ({remaining:.0f}% remaining) · Run /compact to compact & continue"
                )
                color = RED
            elif remaining < 40:
                line = f"{remaining:.0f}% until auto-compact"
                color = YELLOW
            else:
                line = f"{pct:.0f}% context used"
                color = DIM
            print(f"\n{color}{line} · ⬇ {inp:,} in / {out:,} out · {elapsed:.1f}s{RESET}")
        else:
            est = max(1, len(result) // 4)
            print(f"\n{DIM}⬇ ~{est} tokens (估计) · {elapsed:.1f}s{RESET}")
        return result

    def _setup_router(self, model: str, provider: str):
        """Initialize intent classifier + route table. Extensible via register()."""
        from agent.core.intent import IntentClassifier, IntentRouter

        router = IntentRouter()
        try:
            from agent.llm.client import LLMClient

            llm = LLMClient(model=model, provider=provider)
            router.set_classifier(
                IntentClassifier(
                    llm_client=llm,
                    use_llm=True,  # CLI always has LLM
                    fallback_to_legacy=True,  # safety net for API errors
                )
            )
        except BaseException as e:
            # No LLM → heuristic fallback in classifier. Catch BaseException
            # (not just Exception) to handle pydantic/compat errors cleanly.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            pass

        # Register handlers (these are bound methods, can be extended by users)
        router.register("ask", self._run_ask)
        router.register("edit", self._run_edit)
        router.register("agent", self._run_agent)

        self._router = router

    async def _run_ask(self, task: str) -> str:
        """Ask handler — simple Q&A, no file changes."""
        from agent.core.engine import AgentConfig, AgentEngine

        config = AgentConfig(verbose=False, confirm_handler=_confirm_handler)
        engine = AgentEngine(config)
        self._last_engine = engine
        return await self._direct_answer(task, engine, time.time())

    async def _run_edit(self, task: str) -> str:
        """Edit handler — direct execute, no plan phase. Short step limit."""
        from agent.core.engine import AgentConfig, AgentEngine

        from .spinner import StageLabel

        start_time = time.time()
        config = AgentConfig(verbose=False, mode="auto", confirm_handler=_confirm_handler)
        engine = AgentEngine(config)
        self._last_engine = engine

        buffer = []
        _last_tool_content = ""
        edit_label = (task[:28] + "..") if len(task) > 28 else task
        self._spin.start(StageLabel.TOOL.format(tool=edit_label or "edit"))

        _session_turn_estimated = False

        print()
        try:
            async for event in engine.run_stream(task):
                etype = event.get("type")
                if etype == "content":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    token = event.get("content", "")
                    print(_render_markdown_token(token), end="", flush=True)
                    buffer.append(token)
                elif etype == "tool_call":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    icon, label = _tool_icon(event.get("tool_name", ""), event.get("tool_args", {}))
                    print(f"{icon} · {label}")
                    self._spin.start(StageLabel.TOOL.format(tool=event.get("tool_name", "?")))
                elif etype == "tool_result":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    ok = event.get("success")
                    if not ok and event.get("error"):
                        print(f"  {RED}✗{RESET} {event.get('error')[:120]}")
                    elif ok and event.get("content"):
                        brief = event.get("content", "").split("\n")[0][:80]
                        if brief:
                            print(f"  {DIM}{brief}{RESET}")
                        _last_tool_content = event.get("content", "")
                    self._spin.start(StageLabel.THINKING)
                elif etype == "final":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    content = event.get("content", "")
                    if content:
                        buffer.append(content)
                    usage = event.get("usage") or {}
                    if usage:
                        self._session_tokens_in += usage.get("input_tokens", 0)
                        self._session_tokens_out += usage.get("output_tokens", 0)
                    else:
                        _session_turn_estimated = True
                    if event.get("estimated"):
                        _session_turn_estimated = True
                elif etype == "complete":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    content = event.get("content", "")
                    if content:
                        print(f"{YELLOW}{content}{RESET}", flush=True)
                        buffer.append(content)
                elif etype == "error":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    print(f"{RED}{event.get('error', '')}{RESET}")
                    self._spin.start(StageLabel.THINKING)
        except asyncio.CancelledError:
            return "[cancelled]"
        except Exception as e:
            return f"{RED}Error: {e}{RESET}"
        finally:
            await self._spin.stop_async()
            await engine.shutdown()
            if _session_turn_estimated:
                self._session_tokens_estimated_turns += 1

        result = "".join(buffer).strip()
        elapsed = time.time() - start_time
        if not result and _last_tool_content:
            print(f"{DIM}{_last_tool_content.split(chr(10))[0][:100]}{RESET}")
        token_str = (
            f"⬇ {self._session_tokens_in:,} in / {self._session_tokens_out:,} out"
            if not _session_turn_estimated
            else f"⬇ ~{self._session_tokens_out:,} out (估计)"
        )
        print(f"{DIM}── {token_str} · {elapsed:.1f}s{RESET}")
        return result

    async def _run_agent(self, task: str) -> str:
        """Agent handler — LLM decides when to plan via enter_plan_mode/exit_plan_mode tools."""
        return await self._run_task(task)

    async def _run_task(self, task: str, evaluate: bool = False) -> str:
        """Run task with single-phase execution.

        The LLM can call enter_plan_mode/exit_plan_mode tools to explore
        and plan before making changes — no hardcoded plan phase.

        Args:
            task: Task description.
            evaluate: P13-5 — when True, wraps the run with an Evaluator
                Agent that writes SCORE.md + .score.json to the workspace
                after completion.
        """
        from agent.core.engine import AgentConfig, AgentEngine

        start_time = time.time()
        config = AgentConfig(verbose=False, confirm_handler=_confirm_handler)
        engine = AgentEngine(config)
        self._last_engine = engine
        from agent.core.config import config

        mode = config.get("mode")

        # Skip for simple conversational questions
        if _is_simple_question(task):
            return await self._direct_answer(task, engine, start_time)

        # Inject conversation history for multi-turn awareness
        if len(self.history) >= 2:
            history_context = "\n".join(
                f"{h['role']}: {h['content'][:200]}" for h in self.history[-6:]  # last 3 exchanges
            )
            task = f"[Previous conversation]\n{history_context}\n\n[Current task]\n{task}"

        # ── Single-phase streaming execution ──
        from .spinner import StageLabel

        print()
        buffer = []
        # Layer 3: open Live for the spinner phase; on first content event
        # we swap to plain text and close Live. Tool phases use the
        # SpinnerController (separate line) instead.
        _live_ctx = None
        if RICH_AVAILABLE and sys.stdout.isatty():
            try:
                from rich.live import Live
                from rich.spinner import Spinner as _RichSpinner

                _live_render = _RichSpinner("dots", text=StageLabel.THINKING, style="dim")
                _live_ctx = Live(
                    _live_render,
                    console=_RICH,
                    refresh_per_second=12,
                    transient=True,
                )
                _live_ctx.__enter__()
            except Exception:
                _live_ctx = None
        else:
            self._spin.start(StageLabel.THINKING)
        _turn_estimated = False
        _ctx_used = 0
        _ctx_window = 0

        def _close_live():
            nonlocal _live_ctx
            if _live_ctx is not None:
                try:
                    _live_ctx.__exit__(None, None, None)
                except Exception:
                    pass
                _live_ctx = None

        try:
            async for event in engine.run_stream(task):
                etype = event.get("type")

                if etype == "content":
                    if _live_ctx is not None:
                        _close_live()
                    elif self._spin.is_running:
                        await self._spin.stop_async()
                    token = event.get("content", "")
                    print(_render_markdown_token(token), end="", flush=True)
                    buffer.append(token)
                    # Real-time token count into spinner stats (not used now since
                    # spinner is stopped, but kept for future per-token display).

                elif etype == "tool_call":
                    _close_live()
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    name = event.get("tool_name", "")
                    args = event.get("tool_args", {})
                    if buffer:
                        print()
                        buffer.clear()

                    icon, label = _tool_icon(name, args)
                    print(f"{icon} · {label}")
                    # Sub-agent gets its own label so user sees WHICH one
                    if name == "spawn_sub_agent":
                        sub_label = args.get("label") or "subagent"
                        self._spin.start(StageLabel.SUBAGENT.format(name=sub_label))
                    else:
                        self._spin.start(StageLabel.TOOL.format(tool=name))

                elif etype == "tool_result":
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    ok = event.get("success")
                    tool_name = event.get("tool_name", "")
                    if not ok and event.get("error"):
                        if RICH_AVAILABLE:
                            _rich_print_tool_result(
                                tool_name, event.get("error", "")[:120], success=False
                            )
                        else:
                            print(f"  {RED}✗{RESET} {event.get('error')[:120]}")
                    elif ok and tool_name == "exit_plan_mode" and event.get("content"):
                        plan_content = event.get("content", "")
                        # M1 P0: capture the structured metadata so
                        # /plan edit can persist changes back to the
                        # same file. The ExitPlanModeTool now returns
                        # {plan_id, persistence_path, ...} in metadata.
                        meta = event.get("metadata") or {}
                        if meta.get("persistence_path"):
                            self._last_plan_persistence_path = meta["persistence_path"]
                        if RICH_AVAILABLE:
                            from rich.box import ROUNDED
                            from rich.panel import Panel
                            from rich.text import Text

                            body = Text("\n".join(plan_content.split("\n")[:15]))
                            _RICH.print(
                                Panel(
                                    body,
                                    title="[bold cyan]Plan[/bold cyan]",
                                    border_style="cyan",
                                    box=ROUNDED,
                                )
                            )
                        else:
                            print(f"\n{CYAN}╭─ Plan ────────────────────────────────{RESET}")
                            for line in plan_content.split("\n")[:15]:
                                print(f"{CYAN}│{RESET} {line[:76]}")
                            print(f"{CYAN}╰───────────────────────────────────────{RESET}\n")
                    elif ok and tool_name == "todo_write":
                        meta = event.get("metadata") or {}
                        tasks = meta.get("tasks")
                        if tasks:
                            _rich_print_todo(tasks)
                    elif ok:
                        from agent.tools.base import registry as _reg

                        tool = _reg.get(tool_name)
                        if tool:

                            class _FakeResult:
                                success = ok
                                content = event.get("content", "")
                                error = event.get("error", "")
                                metadata = event.get("metadata")

                            summary = tool.render_result(_FakeResult())
                        else:
                            summary = event.get("content", "").split("\n")[0][:80]
                        if summary:
                            display_name = (
                                (tool.user_facing_name or tool_name) if tool else tool_name
                            )
                            _rich_print_tool_result(display_name, summary, success=True)
                    # Reopen Live for the next thinking/content burst
                    if _live_ctx is None and RICH_AVAILABLE and sys.stdout.isatty():
                        try:
                            from rich.live import Live
                            from rich.spinner import Spinner as _RichSpinner

                            _live_render = _RichSpinner(
                                "dots", text=StageLabel.THINKING, style="dim"
                            )
                            _live_ctx = Live(
                                _live_render,
                                console=_RICH,
                                refresh_per_second=12,
                                transient=True,
                            )
                            _live_ctx.__enter__()
                        except Exception:
                            _live_ctx = None
                    else:
                        self._spin.start(StageLabel.THINKING)

                elif etype == "final":
                    _close_live()
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    content = event.get("content", "")
                    if content:
                        buffer.append(content)
                    # Session token accumulator
                    usage = event.get("usage") or {}
                    if usage:
                        self._session_tokens_in += usage.get("input_tokens", 0)
                        self._session_tokens_out += usage.get("output_tokens", 0)
                    else:
                        _turn_estimated = True
                    if event.get("estimated"):
                        _turn_estimated = True
                    # Context usage line — Claude Code style
                    ctx = event.get("context")
                    if ctx:
                        _ctx_used = ctx.get("used", 0)
                        _ctx_window = ctx.get("window", 0)
                        if _ctx_window > 0:
                            pct = _ctx_used / _ctx_window * 100
                            remaining = max(0, 100 - pct)
                            if remaining < 25:
                                line = (
                                    f"Context low ({remaining:.0f}% remaining) "
                                    f"· Run /compact to compact & continue"
                                )
                                color = RED
                            elif remaining < 40:
                                line = f"{remaining:.0f}% until auto-compact"
                                color = YELLOW
                            else:
                                line = f"{pct:.0f}% context used"
                                color = DIM
                            print(f"{color}{line}{RESET}")

                elif etype == "complete":
                    _close_live()
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    content = event.get("content", "")
                    if content:
                        print(f"{YELLOW}{content}{RESET}", flush=True)
                        buffer.append(content)

                elif etype == "error":
                    _close_live()
                    if self._spin.is_running:
                        await self._spin.stop_async()
                    print(f"{RED}Error: {event.get('error', '')}{RESET}")

            _close_live()
            await self._spin.stop_async()

            elapsed = time.time() - start_time
            result_text = "".join(buffer).strip()
            if _turn_estimated:
                self._session_tokens_estimated_turns += 1
                tok_str = f"⬇ ~{self._session_tokens_out:,} out (估计)"
            else:
                tok_str = f"⬇ {self._session_tokens_in:,} in / {self._session_tokens_out:,} out"
            print(f"{DIM}── {tok_str} · {elapsed:.1f}s{RESET}")
            return "".join(buffer)

        except asyncio.CancelledError:
            _close_live()
            await self._spin.stop_async()
            print(f"{YELLOW}⏎ Cancelled{RESET}")
            return "[cancelled]"
        except KeyboardInterrupt:
            _close_live()
            await self._spin.stop_async()
            print(f"{YELLOW}⏎ Cancelled{RESET}")
            return "[cancelled]"
        except Exception as e:
            _close_live()
            await self._spin.stop_async()
            print(f"{DIM}✗ {time.time() - start_time:.1f}s{RESET} {RED}{e}{RESET}")
            return str(e)

        finally:
            await engine.shutdown()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(prog="coding-agent", add_help=False)
    parser.add_argument("task", nargs="?", default=None)
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("-h", "--help", dest="show_help", action="store_true")
    parser.add_argument("--tui", dest="tui_mode", action="store_true", help="Launch Textual TUI")
    parser.add_argument("--cli", dest="cli_mode", action="store_true", help="Use raw CLI (default)")
    # M1 P0: --resume now accepts an optional plan_id. With no argument it
    # resumes the latest SESSION (legacy behaviour, unchanged). With a
    # plan_id argument it resumes a saved PLAN file from
    # ~/.coding-agent/plans/<plan_id>.md and skips the planning phase.
    parser.add_argument(
        "--resume",
        dest="resume",
        nargs="?",
        const="__latest_session__",
        default=None,
        help="Resume last session (no arg) or a saved plan (--resume <plan_id>)",
    )
    parser.add_argument(
        "--plan",
        dest="plan_mode",
        action="store_true",
        help="Enter plan mode (read-only) on launch. Equivalent to AGENT_MODE=plan",
    )
    parser.add_argument(
        "--list-plans", dest="list_plans", action="store_true", help="List saved plan files"
    )
    parser.add_argument(
        "--evaluate",
        dest="evaluate",
        action="store_true",
        help="P13-5: run agent + emit SCORE.md evaluation report after completion",
    )
    parser.add_argument(
        "--list-sessions", dest="list_sessions", action="store_true", help="List saved sessions"
    )
    args, _ = parser.parse_known_args()

    # M1 P0: --plan flag is equivalent to AGENT_MODE=plan. Set BEFORE the
    # engine reads it so any subsequent code path sees the right mode.
    # We touch os.environ (in addition to the engine's mode arg) so the
    # child processes spawned by the dispatcher also inherit it.
    if args.plan_mode or os.environ.get("CODING_AGENT_PLAN") == "1":
        os.environ["AGENT_MODE"] = "plan"
        args.plan_mode = True  # keep flag consistent for downstream code

    # M1 P0: env var shortcut for --resume <plan_id> (handy in CI / scripts
    # where a CLI arg would be awkward). CLI arg wins if both are set.
    env_resume = os.environ.get("CODING_AGENT_RESUME_PLAN")
    if env_resume and args.resume is None:
        args.resume = env_resume

    if args.tui_mode:
        from ui.tui import run_tui

        run_tui()
        return

    if args.list_sessions:
        from agent.core.session import list_sessions as ls

        sessions = ls()
        if not sessions:
            print("No saved sessions.")
        else:
            for s in sessions:
                task_preview = s.get("task", "?")[:60]
                updated = s.get("updated_at", "?")[:19]
                print(f"  {s['_file']}  {updated}  {task_preview}")
        return

    if args.list_plans:
        from agent.tools.plan_mode import _plan_dir

        plan_dir = _plan_dir()
        if not plan_dir.exists():
            print("No saved plans.")
        else:
            files = sorted(plan_dir.glob("*.md"), reverse=True)
            if not files:
                print("No saved plans.")
            else:
                for f in files:
                    print(f"  {f.name}")
        return

    if args.resume is not None:
        # Two resume modes:
        #   --resume                  → latest SESSION (P12-3 behaviour)
        #   --resume <plan_id>        → a specific saved PLAN
        if args.resume == "__latest_session__":
            from agent.core.session import get_latest_session

            s = get_latest_session()
            if not s:
                print("No session to resume.")
                return
            task = s.get("task", "")
            print(f"Resuming: {task[:80]}")
            # P12-3: also pull context from the task state machine (carries
            # completed_steps, known_issues, current_step across crashes).
            try:
                from agent.core.task_state_machine import TaskStateMachine

                tsm = TaskStateMachine()
                reminder = tsm.format_reminder()
                if reminder:
                    args.task = f"[Resumed session]\n{task}\n\n{reminder}"
                else:
                    args.task = f"[Resumed session]\n{task}"
            except Exception:
                args.task = f"[Resumed session]\n{task}"
            # Fall through to single-task mode below
        else:
            # --resume <plan_id>: read the saved plan file and re-enter
            # the run flow with the plan injected as context. The engine
            # sees `args.task` containing the approved plan and skips
            # the planning phase (we also pre-flip the engine out of
            # plan mode so execute-time tools are allowed).
            from agent.tools.plan_mode import _plan_dir

            plan_path = _plan_dir() / f"{args.resume}.md"
            if not plan_path.exists():
                print(f"No saved plan with id: {args.resume}")
                print(f"  (looked at {plan_path})")
                print("Run `coding-agent --list-plans` to see available plans.")
                return
            plan_body = plan_path.read_text(encoding="utf-8")
            # Flip out of plan mode so execute-phase tools are allowed.
            os.environ["AGENT_MODE"] = "default"
            print(f"Resuming plan: {args.resume}")
            args.task = (
                f"[Resumed plan: {args.resume}]\n\n"
                f"Approved plan to execute:\n\n{plan_body}\n\n"
                f"Proceed to implement the plan above. Skip the planning "
                f"phase — the plan is already approved."
            )
            # Fall through to single-task mode below

    if args.task:
        # Single task mode — delegate to _run_async for set_debug(False) +
        # slow_callback_duration + _silent_handler cleanup (matches cli.py:1220, :1238).
        cli = SimpleCLI()
        from agent.core.config import config

        cli._setup_router(config.get("model"), config.get("provider"))
        coro = (
            cli._router.route(args.task)
            if cli._router
            else cli._run_task(args.task, evaluate=args.evaluate)
        )
        result = _run_async(coro)
        if args.print_mode:
            print(result)
        return

    if args.show_help or not sys.stdin.isatty():
        print_banner()
        print(f"{DIM}Usage: coding-agent [task] [-p] [--help] [--plan] [--resume [plan_id]]")
        print("Examples:")
        print("  coding-agent                              # Interactive mode")
        print('  coding-agent "write hello.py"             # Single task')
        print('  coding-agent -p "task"                     # Print result only')
        print('  coding-agent --plan "implement feature X"  # Start in plan mode')
        print("  coding-agent --resume                      # Resume last session")
        print("  coding-agent --resume plan-foo-123-abc     # Resume saved plan")
        print("  coding-agent --list-plans                 # List saved plans")
        print()
        print(f"{BOLD}Slash commands:{RESET}")
        print(f"  {CYAN}/help{RESET}             Show available commands")
        print(f"  {CYAN}/clear{RESET}            Clear conversation")
        print(f"  {CYAN}/plan{RESET}             Switch to plan (read-only) mode")
        print(f"  {CYAN}/commit{RESET}           Commit all changes")
        print(f"  {CYAN}/model{RESET} <name>     Show or switch model")
        print(f"  {CYAN}/mode{RESET} <mode>      Show or switch permission mode")
        print(f"  {CYAN}/memory{RESET} [search]  Show or search long-term memory")
        print(f"  {CYAN}/status{RESET}           Show agent status")
        print(f"  {CYAN}/context{RESET} [full]   Show context window usage")
        print(f"  {CYAN}/review{RESET} [diff]    Review git changes")
        print(f"  {CYAN}/undo{RESET} [commit]    Undo changes or last commit")
        print(f"  {CYAN}/quit{RESET}             Exit the coding agent")
        return

    cli = SimpleCLI()
    cli.run()


if __name__ == "__main__":
    main()
