"""Built-in slash commands for the coding agent.

Command handlers are async functions with signature:
    async def handler(args: str, ctx: dict) -> str

ctx provides: engine, cli, model, provider, mode, workspace
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path

from .base import SlashCommand, registry

# ── Helper ──────────────────────────────────────────────────────────────────


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m"


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _cyan(text: str) -> str:
    return f"\033[36m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


# ── /clear ──────────────────────────────────────────────────────────────────


async def _handle_clear(args: str, ctx: dict) -> str:
    """Clear the current conversation context and memory."""
    engine = ctx.get("engine")
    cli = ctx.get("cli")
    if engine:
        engine.memory.clear_working_memory()
    if cli:
        cli.history = []
        cli._input_history = []
        cli.file_context = []
    return f"{_green('✓')} Conversation cleared. Fresh start."


registry.register(
    SlashCommand(
        name="clear",
        description="Clear the current conversation and start fresh",
        usage="/clear",
        aliases=["reset"],
    )
)
registry._commands["clear"]._handler = _handle_clear


# ── /plan ───────────────────────────────────────────────────────────────────


# Fields that /plan edit is allowed to mutate. Anything else is rejected
# to keep the command surface tight — wildcards would let a typo silently
# create a new attribute.
_PLAN_EDITABLE_FIELDS = ("description", "tool_hint", "expected_outcome")


def _apply_plan_edit(plan, step_num: int, field: str, new_value: str) -> tuple[bool, str]:
    """Mutate ``plan.steps[step_num-1].<field> = new_value``.

    Returns (ok, message). The caller decides how to render the message.

    Why return a tuple instead of raising: the slash-command handler is
    already a string-in / string-out pipeline, and tests want to assert on
    both branches without dealing with exceptions.
    """
    if not (1 <= step_num <= len(plan.steps)):
        return False, f"Step {step_num} does not exist (plan has {len(plan.steps)} steps)."

    if field not in _PLAN_EDITABLE_FIELDS:
        allowed = ", ".join(_PLAN_EDITABLE_FIELDS)
        return False, f"Field '{field}' is not editable. Allowed: {allowed}."

    step = plan.steps[step_num - 1]
    setattr(step, field, new_value)
    return True, f"Step {step_num}.{field} updated to: {new_value}"


async def _handle_plan_from_spec(rest: str, ctx: dict) -> str:
    """M2 P0: bridge SPECS.md → ExecutionPlan via SpecPlanAdapter.

    Two modes:
      * no phase_id argument → list eligible phases from SPECS.md
      * phase_id argument → build the plan, stash it on cli._last_plan
        so /plan show / /plan edit work on it
    """
    from agent.core.plan_review import review_plan
    from agent.core.spec_plan_adapter import (
        SpecPlanAdapterError,
        from_spec,
        list_eligible_phases,
    )

    cli = ctx.get("cli")
    workspace = ctx.get("workspace") or "."
    phase_id = rest.strip()

    if not phase_id:
        try:
            eligible = list_eligible_phases(Path(workspace))
        except Exception as exc:  # pragma: no cover
            return f"{_dim(f'Failed to read SPECS.md: {exc}')}"
        if not eligible:
            return (
                f"{_dim('No eligible phases in SPECS.md.')}\n"
                f"{_dim('A phase is eligible when its status is planned or partial.')}"
            )
        lines = [f"{_green('Eligible phases:')} {', '.join(eligible)}"]
        lines.append(
            f"{_dim('Usage: /plan from-spec <phase_id>   e.g. /plan from-spec {eligible[0]}')}"
        )
        return "\n".join(lines)

    try:
        plan = from_spec(Path(workspace), phase_id)
    except SpecPlanAdapterError as exc:
        return f"{_dim(str(exc))}"

    # M2 P0: run the static plan review and surface its summary inline.
    # Full report is attached to plan.review_notes so /plan show renders
    # it. We don't block on reject findings — user retains final say.
    report = review_plan(plan)
    plan.review_notes = report.to_markdown()

    if cli is not None:
        cli._last_plan = plan

    pending = sum(1 for s in plan.steps if s.status != "done")
    return (
        f"{_green('✓')} Built plan from SPECS.md phase {phase_id}: "
        f"{_bold(plan.title or plan.summary or plan.task)}\n"
        f"  {len(plan.steps)} steps ({pending} pending), "
        f"{len(plan.acceptance_criteria)} ACs, plan_id={_cyan(plan.plan_id)}\n"
        f"  {_dim(f'Review: {report.summary}')}\n"
        f"  {_dim('Use /plan show to view, /plan edit to refine, /plan accept to execute.')}"
    )


async def _handle_plan(args: str, ctx: dict) -> str:
    """Switch to or manage plan mode."""
    engine = ctx.get("engine")
    sub = args.strip()
    # Lowercase only the leading token so we don't mangle user content
    # like "edit 3 Implement FEATURE X" (description case matters).
    first_token, _, rest = sub.partition(" ")
    first_token_lc = first_token.lower()

    if first_token_lc == "accept":
        # Accept pending plan (only meaningful after plan is generated)
        os.environ["AGENT_MODE"] = "default"
        if engine:
            engine.permissions.mode = type(engine.permissions.mode)("default")
        return f"{_green('✓')} Plan accepted. Execute with next task or switch mode: {_dim('/mode default')}"

    if first_token_lc == "reject":
        return f"{_dim('Plan discarded. Type a new task to re-plan.')}"

    if first_token_lc == "show":
        # Show last plan if available
        cli = ctx.get("cli")
        if cli and cli._last_plan:
            return cli._last_plan.to_markdown()
        return f"{_dim('No plan yet. Type a task to generate one.')}"

    if first_token_lc == "from-spec":
        # M2 P0: bridge SPECS.md → ExecutionPlan via SpecPlanAdapter.
        return await _handle_plan_from_spec(rest, ctx)

    if first_token_lc == "edit":
        # Two forms are accepted:
        #   /plan edit <N> <new description>           (legacy, free-form)
        #   /plan edit <N> <field> <new value>         (structured)
        # where <field> ∈ {description, tool_hint, expected_outcome}.
        # The structured form takes precedence when the second token is a
        # known field name; otherwise the second token is treated as the
        # start of the description (legacy behaviour, preserved for users
        # who learnt the original command in earlier versions).
        cli = ctx.get("cli")
        if not cli or not cli._last_plan:
            return f"{_dim('No plan to edit. Generate one first.')}"
        plan = cli._last_plan

        # Split the args (rest) into up to 3 parts.
        parts = rest.split(maxsplit=2) if rest else []
        if len(parts) < 2:
            return (
                f"Usage: {_dim('/plan edit <step> <new description>')}\n"
                f"       {_dim('/plan edit <step> <field> <new value>')}  "
                f"(field ∈ {', '.join(_PLAN_EDITABLE_FIELDS)})"
            )

        try:
            step_num = int(parts[0])
        except ValueError:
            return f"{_dim('First argument must be a step number.')}"

        # Decide between legacy and structured form
        if len(parts) == 2:
            # Legacy: edit <N> <description>
            field, new_value = "description", parts[1]
        else:
            candidate_field = parts[1].lower()
            if candidate_field in _PLAN_EDITABLE_FIELDS:
                field, new_value = candidate_field, parts[2]
            else:
                # Treat the entire tail as the description (preserves
                # legacy behaviour for users with spaces in descriptions).
                field, new_value = "description", " ".join(parts[1:])

        ok, msg = _apply_plan_edit(plan, step_num, field, new_value)
        if not ok:
            return f"{_dim(msg)}"

        # Re-persist to the on-disk file when we know which one. M2 will
        # wire ExecutionPlan.plan_id properly; for now we just track via
        # the CLI's last persistence_path (set by the ExitPlanModeTool
        # metadata in the new design).
        persistence_path = getattr(cli, "_last_plan_persistence_path", None)
        if persistence_path and Path(persistence_path).exists():
            try:
                # Rewrite the body section under the existing frontmatter
                body = plan.to_markdown()
                existing = Path(persistence_path).read_text(encoding="utf-8")
                if "\n---\n" in existing:
                    head, _, _ = existing.partition("\n---\n")
                    Path(persistence_path).write_text(
                        head + "\n---\n\n" + body + "\n", encoding="utf-8"
                    )
                else:
                    Path(persistence_path).write_text(body + "\n", encoding="utf-8")
                return f"{_green('✓')} {msg}\n{_dim(f'Persisted to {persistence_path}')}"
            except Exception as exc:
                # Persistence is best-effort; the in-memory plan is updated
                # regardless so the next /plan show reflects the change.
                return f"{_green('✓')} {msg}\n{_dim(f'(disk write failed: {exc})')}"

        return f"{_green('✓')} {msg}"

    # Default: switch to plan mode
    new_mode = "plan"
    if engine:
        engine.permissions.mode = type(engine.permissions.mode)(new_mode)
    os.environ["AGENT_MODE"] = new_mode
    return (
        f"{_green('✓')} Switched to {_bold('plan')} mode.\n"
        f"{_dim('Plan mode: read-only. Agent analyzes and generates plans, but does not edit.')}\n"
        f"{_dim('Type /plan show — view last plan  |  /plan accept — approve  |  /plan edit N desc — modify step')}"
    )


registry.register(
    SlashCommand(
        name="plan",
        description="Enter plan mode or manage plans (accept / reject / edit / show)",
        usage="/plan [accept|reject|edit N desc|show]",
    )
)
registry._commands["plan"]._handler = _handle_plan


# ── /commit ─────────────────────────────────────────────────────────────────


async def _handle_commit(args: str, ctx: dict) -> str:
    """Stage changes and create a git commit with LLM-generated message."""
    cwd = ctx.get("workspace", ".")
    engine = ctx.get("engine")

    try:
        # Check for changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        changed = stdout.decode().strip()

        if not changed:
            return f"{_dim('Nothing to commit (working tree clean)')}"

        # Show diff stat
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--stat",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        diff_stat = stdout.decode().strip()

        lines = [
            f"{_bold('Changes to commit:')}",
            diff_stat or changed,
            "",
        ]

        # Use smart_commit tool for LLM-generated message
        from agent.tools.git_smart import SmartCommitTool

        tool = SmartCommitTool()
        result = await tool.execute(message=args.strip() or "", cwd=cwd)

        if result.success:
            lines.append(result.content)
        else:
            lines.append(f"Error: {result.error}")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


registry.register(
    SlashCommand(
        name="commit",
        description="Stage all changes and create a git commit with auto-generated message",
        usage="/commit",
        aliases=["cmt"],
    )
)
registry._commands["commit"]._handler = _handle_commit


# ── /help ───────────────────────────────────────────────────────────────────


async def _handle_help(args: str, ctx: dict) -> str:
    """Show available commands."""
    cmds = registry.list_all()
    lines = [
        f"{_bold('Available commands:')}",
        "",
    ]
    for cmd in cmds:
        aliases_str = f" ({', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
        lines.append(f"  {_cyan('/' + cmd.name):<20} {cmd.description}{_dim(aliases_str)}")

    lines.append("")
    lines.append(f"  {_dim('Type /<command> to use, or just type a task to send to the agent.')}")
    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="help",
        description="Show this help message",
        usage="/help",
        aliases=["h", "?"],
    )
)
registry._commands["help"]._handler = _handle_help


# ── /model ──────────────────────────────────────────────────────────────────


async def _handle_model(args: str, ctx: dict) -> str:
    """Show or switch the current model."""
    current = ctx.get("model", "unknown")
    current_provider = ctx.get("provider", "auto")

    if not args.strip():
        return f"Current model: {_bold(current)} (provider: {current_provider})\nUse {_dim('/model <model-name>')} to switch."

    new_model = args.strip()
    os.environ["DEFAULT_MODEL"] = new_model
    return (
        f"{_green('✓')} Model set to {_bold(new_model)}\n"
        f"{_dim('Note: changes take effect on the next task (recreates engine).')}"
    )


registry.register(
    SlashCommand(
        name="model",
        description="Show or switch the current LLM model",
        usage="/model [model-name]",
    )
)
registry._commands["model"]._handler = _handle_model


# ── /mode ───────────────────────────────────────────────────────────────────


async def _handle_mode(args: str, ctx: dict) -> str:
    """Show or switch the permission mode."""
    valid_modes = {"plan", "default", "auto", "bypass"}
    engine = ctx.get("engine")

    if not args.strip():
        current = ctx.get("mode", "default")
        desc = {
            "plan": "Read-only exploration",
            "default": "Interactive — risky operations need confirmation",
            "auto": "Auto-approve low/medium risk, confirm high risk",
            "bypass": "Auto-approve everything (except critical)",
        }
        lines = [
            f"Current mode: {_bold(current)} — {desc.get(current, '')}",
            "",
            "Available modes:",
        ]
        for m in sorted(valid_modes):
            marker = _green(" ●") if m == current else "  "
            lines.append(f"  {marker} {_bold(m):<12} {_dim(desc.get(m, ''))}")
        return "\n".join(lines)

    new_mode = args.strip().lower()
    if new_mode not in valid_modes:
        return f"Invalid mode '{new_mode}'. Valid: {', '.join(sorted(valid_modes))}"

    if engine:
        engine.permissions.mode = type(engine.permissions.mode)(new_mode)
    os.environ["AGENT_MODE"] = new_mode
    return f"{_green('✓')} Mode switched to {_bold(new_mode)}"


registry.register(
    SlashCommand(
        name="mode",
        description="Show or switch permission mode (plan / default / auto / bypass)",
        usage="/mode [plan|default|auto|bypass]",
        aliases=["permission"],
    )
)
registry._commands["mode"]._handler = _handle_mode


# ── /memory ──────────────────────────────────────────────────────────────────


async def _handle_memory(args: str, ctx: dict) -> str:
    """Show or manage long-term memory.

    Subcommands:
      (no args)            — show status + last 10 entries
      clear                — wipe long-term + vector memory
      search <query>       — semantic search over memory.md
      add <key> <value>    — append a fact (pinned, exempt from 50-entry cap)
      forget <key>         — remove an entry by key
    """
    engine = ctx.get("engine")
    if not engine:
        return "No engine available."

    mem = engine.memory
    long_term = mem.long_term
    working_count = len(mem.working_memory)
    summary_count = len(mem.summaries)
    vector_count = 0
    try:
        vector_count = mem.vector_memory.count()
    except Exception:
        pass

    args = args.strip()

    if args == "clear":
        mem.long_term = ""
        mem._save_long_term()
        try:
            mem.vector_memory.clear()
        except Exception:
            pass
        return f"{_green('✓')} Long-term memory cleared."

    if args.startswith("search "):
        query = args[7:]
        results = mem.search_long_term(query, top_k=5)
        if not results:
            return f"No results for '{query}'."
        lines = [f"Memory search results for '{_bold(query)}':", ""]
        for key, value, score in results:
            lines.append(f"  [{score:.2f}] {_cyan(key)}: {value[:80]}")
        return "\n".join(lines)

    # PR-14: /memory add <key> <value> — pinned, exempt from 50-entry LRU
    if args.startswith("add "):
        rest = args[4:].strip()
        # Split on first whitespace: "add foo bar baz" → key=foo, value="bar baz"
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return f"{_yellow('usage:')} /memory add <key> <value>"
        key, value = parts[0], parts[1]
        if not key.replace("_", "").replace(".", "").replace("-", "").isalnum():
            return f"{_yellow('key must be alphanumeric (with _ . -):')} {key!r}"
        mem.remember(key, value, pinned=True)
        return f"{_green('✓')} Pinned to long-term memory: {key} = {value[:60]}"

    # PR-14: /memory forget <key> — remove an entry
    if args.startswith("forget "):
        key = args[7:].strip()
        if not key:
            return f"{_yellow('usage:')} /memory forget <key>"
        # Try to find and remove a matching entry in memory.md
        lines_in = mem.long_term.strip().split("\n") if mem.long_term else []
        kept = []
        removed = False
        for line in lines_in:
            # Match both pinned "📌 key: ..." and unpinned "key: ..."
            stripped = line.lstrip("- ").lstrip("📌 ").lstrip()
            if not removed and stripped.startswith(f"{key}:"):
                removed = True
                continue
            kept.append(line)
        if removed:
            mem.long_term = "\n".join(kept) + ("\n" if kept else "")
            mem._save_long_term()
            return f"{_green('✓')} Forgot '{key}'."
        return f"{_yellow('No entry matching key:')} {key}"

    lines = [
        f"{_bold('Memory status:')}",
        f"  Working memory: {working_count} messages",
        f"  Summaries:      {summary_count} compressed blocks",
        f"  Long-term:      {len(long_term.splitlines()) if long_term else 0} entries",
        f"  Vector store:   {vector_count} records",
    ]
    if long_term:
        lines.append("")
        lines.append(f"{_bold('Long-term memory (last 10):')}")
        entries = long_term.strip().split("\n")[-10:]
        for e in entries:
            lines.append(f"  {_dim(e[:100])}")
        lines.append("")
        help_text = (
            "/memory clear  |  /memory search <q>  |  "
            "/memory add <key> <value>  |  /memory forget <key>"
        )
        lines.append(f"  {_dim(help_text)}")

    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="memory",
        description="Show or manage long-term memory",
        usage="/memory [clear | search <query>]",
        aliases=["mem"],
    )
)
registry._commands["memory"]._handler = _handle_memory


# ── /status ──────────────────────────────────────────────────────────────────


async def _handle_status(args: str, ctx: dict) -> str:
    """Show current agent status."""
    engine = ctx.get("engine")
    model = ctx.get("model", "unknown")
    mode = ctx.get("mode", "unknown")
    provider = ctx.get("provider", "unknown")
    workspace = ctx.get("workspace", ".")

    wm_count = len(engine.memory.working_memory) if engine else 0
    summary_count = len(engine.memory.summaries) if engine else 0
    project_dir = engine.current_project_dir if engine else None
    has_project_ctx = bool(engine.project_context) if engine else False
    spec_phase = engine.spec_context.active_phase if engine and engine.spec_context else None

    # Git status
    git_branch = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        git_branch = stdout.decode().strip()
    except Exception:
        pass

    lines = [
        f"{_bold('Coding Agent Status')}",
        f"  Model:      {_cyan(model)} ({provider})",
        f"  Mode:       {_bold(mode)}",
        f"  Workspace:  {_dim(workspace)}",
    ]
    if git_branch:
        lines.append(f"  Git:        {_cyan(git_branch)}")
    if project_dir:
        lines.append(f"  Project:    {_bold(project_dir)}/")
    lines.append(
        f"  Context:    {_green('CODING_AGENT.md loaded') if has_project_ctx else _dim('no CODING_AGENT.md')}"
    )
    if spec_phase:
        lines.append(
            f"  Spec phase: {_cyan(f'Phase {spec_phase.number}: {spec_phase.name}')} ({spec_phase.status})"
        )
    lines.extend(
        [
            f"  Memory:     {wm_count} working messages, {summary_count} summaries",
            f"  Timestamp:  {datetime.now().strftime('%H:%M:%S')}",
        ]
    )
    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="status",
        description="Show agent status (model, mode, memory, git branch)",
        usage="/status",
        aliases=["st", "info"],
    )
)
registry._commands["status"]._handler = _handle_status


# ── /context ─────────────────────────────────────────────────────────────────


async def _handle_context(args: str, ctx: dict) -> str:
    """Show the current conversation context."""
    engine = ctx.get("engine")
    if not engine:
        return "No engine available."

    messages = engine.memory.get_messages()
    if not messages:
        return f"{_dim('No conversation context yet.')}"

    # Use real LLM-reported token count when available; fall back to local
    # estimate only if the engine has no recorded usage yet.
    real_in = engine.total_input_tokens
    real_out = engine.total_output_tokens
    real_total = real_in + real_out
    estimated_flag = engine.last_usage_estimated
    if real_total > 0 and not estimated_flag:
        estimated_tokens = real_total
        tokens_label = f"{estimated_tokens:,} (real)"
    else:
        estimated_tokens = engine.memory._estimate_tokens()
        tokens_label = f"{estimated_tokens:,} (估计)"
    max_tokens = engine.memory.max_tokens
    pct = min(100, int(estimated_tokens / max_tokens * 100)) if max_tokens else 0

    bar_width = 20
    filled = int(bar_width * pct / 100)
    bar = _green("█" * filled) + _dim("░" * (bar_width - filled))

    lines = [
        f"{_bold('Context window:')} [{bar}] {tokens_label}/{max_tokens} tokens ({pct}%)",
        f"  Messages: {len(messages)}",
        "",
    ]

    if args.strip() == "full":
        lines.append(f"{_bold('Full context:')}")
        for i, msg in enumerate(messages[-20:]):
            role_prefix = {"system": "S", "user": "U", "assistant": "A", "tool": "T"}.get(
                msg.role, "?"
            )
            content_preview = msg.content[:100].replace("\n", " ")
            lines.append(f"  [{role_prefix}] {_dim(content_preview)}")
    else:
        lines.append(f"  {_dim('Use /context full to see all messages.')}")

    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="context",
        description="Show context window usage and recent messages",
        usage="/context [full]",
        aliases=["ctx"],
    )
)
registry._commands["context"]._handler = _handle_context


# ── /review ──────────────────────────────────────────────────────────────────


async def _handle_review(args: str, ctx: dict) -> str:
    """Show git diff of current changes."""
    cwd = ctx.get("workspace", ".")
    try:
        # Staged changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--cached",
            "--stat",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        staged = stdout.decode().strip()

        # Unstaged changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--stat",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        unstaged = stdout.decode().strip()

        lines = [f"{_bold('Code review:')}", ""]
        if staged:
            lines.append(f"{_green('Staged:')}")
            lines.append(staged)
            lines.append("")
        if unstaged:
            lines.append(f"{_yellow('Unstaged:')}")
            lines.append(unstaged)
            lines.append("")
        if not staged and not unstaged:
            lines.append(f"{_dim('No changes to review.')}")
        elif args.strip() == "diff":
            proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            lines.append(stdout.decode())

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


registry.register(
    SlashCommand(
        name="review",
        description="Review git changes (diff stat, or /review diff for full diff)",
        usage="/review [diff]",
        aliases=["diff"],
    )
)
registry._commands["review"]._handler = _handle_review


# ── /undo ────────────────────────────────────────────────────────────────────


async def _handle_undo(args: str, ctx: dict) -> str:
    """Undo the last git commit, discard working changes, or revert a profile edit.

    Subcommands:
      /undo (or /undo changes) — discard unstaged working changes
      /undo commit             — undo last commit (keep changes staged)
      /undo profile            — revert the most recent UserProfile change (L4)
    """
    cwd = ctx.get("workspace", ".")
    try:
        target = args.strip().lower() if args.strip() else "changes"

        # L4: revert a profile mutation
        if target == "profile":
            from ..core.user_profile import UserProfile

            engine = ctx.get("engine")
            profile = (
                engine.user_profile
                if engine and getattr(engine, "user_profile", None)
                else UserProfile.load()
            )
            record = profile.undo_last()
            if record is None:
                return (
                    f"{_yellow('Nothing to undo.')}\n" f"{_dim('Profile has no recorded changes.')}"
                )
            before_repr = record.before if record.before is not None else "(none)"
            after_repr = record.after if record.after is not None else "(none)"
            return (
                f"{_green('✓ Reverted:')} {record.action} on {record.key}\n"
                f"  before: {before_repr!r}\n"
                f"  after:  {after_repr!r}\n"
                f"  source: {record.source}\n"
                f"  Profile: {profile.summary()}"
            )

        if target == "commit":
            proc = await asyncio.create_subprocess_exec(
                "git",
                "reset",
                "--soft",
                "HEAD~1",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                return f"{_green('✓')} Undid last commit (changes kept staged)."
            return f"Error: {stderr.decode()}"

        # Default: discard unstaged changes
        if target in ("changes", "unstaged"):
            proc = await asyncio.create_subprocess_exec(
                "git",
                "checkout",
                "--",
                ".",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return f"{_green('✓')} Discarded unstaged changes.\n{_dim('Use /undo commit to undo last commit. Use /undo profile to revert a profile change.')}"

        return f"Usage: {_dim('/undo [changes|commit|profile]')}"

    except Exception as e:
        return f"Error: {e}"


async def _handle_quit(args: str, ctx: dict) -> str:
    """Handle /quit command."""
    cli = ctx.get("cli")
    if cli:
        cli._should_quit = True
    return "Goodbye."


async def _handle_fork(args: str, ctx: dict) -> str:
    """Handle /fork command."""
    from agent.core.session import make_session_id, save_session

    cli = ctx.get("cli")
    if not cli:
        return "No CLI context"
    sid = make_session_id()
    data = {
        "session_id": sid,
        "created_at": sid[:15],
        "task": args or "forked session",
        "history": cli.history,
    }
    save_session(sid, data)
    return f"Forked to session {sid}. Use coding-agent --resume to restore."


async def _handle_switch(args: str, ctx: dict) -> str:
    """Handle /switch command."""
    from agent.core.session import load_session

    cli = ctx.get("cli")
    if not cli:
        return "No CLI context"
    sid = args.strip() if args else ""
    if not sid:
        return "Usage: /switch <session_id>"
    data = load_session(sid)
    if not data:
        return f"Session {sid} not found"
    cli.history = data.get("history", [])
    return f"Switched to session {sid} ({len(cli.history)} messages loaded)"


registry.register(
    SlashCommand(
        name="quit",
        description="Exit the coding agent",
        usage="/quit",
    )
)
registry._commands["quit"]._handler = _handle_quit

registry.register(
    SlashCommand(
        name="fork",
        description="Save current session as a branch",
        usage="/fork [label]",
    )
)
registry._commands["fork"]._handler = _handle_fork

registry.register(
    SlashCommand(
        name="switch",
        description="Switch to a saved session",
        usage="/switch <session_id>",
    )
)
registry._commands["switch"]._handler = _handle_switch

registry.register(
    SlashCommand(
        name="undo",
        description="Undo last changes (discard unstaged) or undo last commit",
        usage="/undo [changes|commit]",
    )
)
registry._commands["undo"]._handler = _handle_undo


# ── /orchestrate (PR-07) ───────────────────────────────────────────


async def _handle_orchestrate(args: str, ctx: dict) -> str:
    """PR-07: Run a complex task through the Orchestrator PM Agent.

    The orchestrator decomposes the task into a DAG of subtasks assigned
    to specialized roles (code / test / reviewer / devops), then merges
    the results into a final report.
    """
    task = args.strip()
    if not task:
        return (
            f"{_yellow('Usage:')} /orchestrate <task description>\n"
            f"Example: /orchestrate implement JWT auth with tests and review"
        )
    engine = ctx.get("engine")
    if engine is None:
        return f"{_yellow('⚠ No engine available — cannot orchestrate.')}"
    if engine.llm is None:
        return f"{_yellow('⚠ Engine has no LLM configured — orchestrator unavailable.')}"
    try:
        result = await engine.run_with_orchestrator(task)
    except Exception as e:
        return f"{_yellow('⚠ Orchestrator failed:')} {e}"
    return result


registry.register(
    SlashCommand(
        name="orchestrate",
        description=(
            "Run a complex task through the Orchestrator PM Agent "
            "(decomposes into code/test/reviewer/devops subtasks)"
        ),
        usage="/orchestrate <task>",
    )
)
registry._commands["orchestrate"]._handler = _handle_orchestrate


# ── /audit (PR-08) ─────────────────────────────────────────────────


async def _handle_audit(args: str, ctx: dict) -> str:
    """PR-08: Inspect the append-only audit log.

    Subcommands:
        /audit stats              — aggregate counts (total, by_action, by_tool, by_agent)
        /audit query [N=20]       — last N records (JSON, default 20)
        /audit rotate [DAYS=30]   — archive logs older than DAYS days
    """
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "stats"
    try:
        from ..core.audit_log import get_audit_logger

        audit = get_audit_logger()
    except Exception as e:
        return f"{_yellow('⚠ Failed to load audit logger:')} {e}"

    if sub == "stats":
        s = audit.stats()
        lines = [
            f"{_bold('Audit stats')}",
            f"  total entries: {s['total_entries']}",
            f"  log dir:       {s['log_dir']}",
            f"  archive dir:   {s['archive_dir']}",
        ]
        if s["by_action"]:
            lines.append("  by_action:")
            for k, v in sorted(s["by_action"].items(), key=lambda x: -x[1]):
                lines.append(f"    {k:<20s} {v}")
        if s.get("by_tool"):
            lines.append("  by_tool (top 10):")
            top = sorted(s["by_tool"].items(), key=lambda x: -x[1])[:10]
            for k, v in top:
                lines.append(f"    {k:<20s} {v}")
        if s.get("by_agent"):
            lines.append("  by_agent:")
            for k, v in sorted(s["by_agent"].items(), key=lambda x: -x[1]):
                lines.append(f"    {k:<20s} {v}")
        return "\n".join(lines)

    if sub == "query":
        try:
            n = int(parts[1]) if len(parts) > 1 else 20
        except ValueError:
            n = 20
        records = audit.query(limit=n)
        if not records:
            return _yellow("(audit log empty)")
        import json

        return json.dumps(records[-n:], indent=2, ensure_ascii=False, default=str)

    if sub == "rotate":
        try:
            days = int(parts[1]) if len(parts) > 1 else 30
        except ValueError:
            days = 30
        count = audit.rotate(retention_days=days)
        return f"Rotated {count} file(s) older than {days} days"

    return (
        f"{_yellow('Usage:')} /audit [stats|query|rotate]\n"
        f"  /audit stats              — counts by action/tool/agent\n"
        f"  /audit query [N=20]       — last N records as JSON\n"
        f"  /audit rotate [DAYS=30]   — archive old logs"
    )


registry.register(
    SlashCommand(
        name="audit",
        description="Inspect the append-only audit log (stats/query/rotate)",
        usage="/audit [stats|query|rotate]",
    )
)
registry._commands["audit"]._handler = _handle_audit


# ── /dual-review (PR-11) ────────────────────────────────────────────


async def _handle_dual_review(args: str, ctx: dict) -> str:
    """PR-11: Show dual-agent review stats and current configuration.

    Subcommands:
        /dual-review                — show stats + high-risk tool list
        /dual-review reset          — reset stats counters (test/debug)
    """
    engine = ctx.get("engine")
    if engine is None or engine.dual_review is None:
        return _yellow("Dual-agent review is disabled (enable_dual_review=False or no engine)")

    mgr = engine.dual_review
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "stats"

    if sub == "reset":
        mgr.reviews_run = 0
        mgr.reviews_approved = 0
        mgr.reviews_rejected = 0
        mgr.reviews_user_required = 0
        mgr.reviews_rate_limited = 0
        mgr.rate_limiter.reset()
        return _green("✓ Dual-review stats reset.")

    # Default: stats
    high_risk = ", ".join(sorted(mgr.HIGH_RISK_TOOLS))
    return (
        f"{_bold('Dual-agent review stats')}\n"
        f"  enabled:             True\n"
        f"  primary model:       {mgr.primary_model}\n"
        f"  secondary model:     {mgr.secondary_model}\n"
        f"  rate limit:          {mgr.rate_limiter.max} / {mgr.rate_limiter.window}s "
        f"(used: {mgr.rate_limiter.used()})\n"
        f"  reviews_run:         {mgr.reviews_run}\n"
        f"  reviews_approved:    {mgr.reviews_approved}\n"
        f"  reviews_rejected:    {mgr.reviews_rejected}\n"
        f"  reviews_user_req:    {mgr.reviews_user_required}\n"
        f"  reviews_rate_limit:  {mgr.reviews_rate_limited}\n"
        f"\n{_bold('High-risk tools ({0}):')} {high_risk}".format(len(mgr.HIGH_RISK_TOOLS))
    )


registry.register(
    SlashCommand(
        name="dual-review",
        description="Show dual-agent review stats (PR-11) for high-risk tool calls",
        usage="/dual-review [reset]",
    )
)
registry._commands["dual-review"]._handler = _handle_dual_review


# ── /ab (PR-12) ─────────────────────────────────────────────────────


async def _handle_ab(args: str, ctx: dict) -> str:
    """PR-12: A/B testing framework — list/create/status/analyze/conclude.

    Subcommands:
        /ab                       — summary of all experiments
        /ab list                  — list all experiments with status
        /ab status <exp_id>       — show one experiment's full state
        /ab analyze <exp_id>      — run analysis on a running experiment
        /ab conclude <exp_id>     — conclude a running experiment
        /ab abandon <exp_id>      — mark a running experiment as abandoned
    """
    engine = ctx.get("engine")
    if engine is None or engine.ab_test is None:
        return _yellow("A/B testing is disabled (ab_test_enabled=False or no engine)")

    mgr = engine.ab_test
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "summary"

    if sub == "summary":
        s = mgr.stats()
        return (
            f"{_bold('A/B test summary')}\n"
            f"  exp dir:        {s['exp_dir']}\n"
            f"  total:          {s['experiment_count']}\n"
            f"  running:        {s['running']}\n"
            f"  completed:      {s['completed']}\n"
            f"  abandoned:      {s['abandoned']}"
        )

    if sub == "list":
        exps = mgr.list()
        if not exps:
            return _yellow("(no experiments)")
        lines = [f"{_bold(f'{len(exps)} experiment(s):')}"]
        for e in exps:
            winner = f"  winner: {e.winner}" if e.winner else ""
            lines.append(
                f"  {e.id:<20s} {e.status:<10s} {e.name}  "
                f"({len(e.variants)} variants, target={e.target}:{e.target_key}){winner}"
            )
        return "\n".join(lines)

    if sub == "status":
        if len(parts) < 2:
            return _yellow("Usage: /ab status <exp_id>")
        exp = mgr.get(parts[1])
        if exp is None:
            return _yellow(f"Experiment {parts[1]!r} not found")
        variants = ", ".join(f"{v.id}({v.name},w={v.weight})" for v in exp.variants)
        winner = f"  winner: {exp.winner}" if exp.winner else ""
        return (
            f"{_bold(f'Experiment {exp.id}')}\n"
            f"  name:        {exp.name}\n"
            f"  description: {exp.description}\n"
            f"  status:      {exp.status}\n"
            f"  target:      {exp.target}:{exp.target_key}\n"
            f"  variants:    {variants}\n"
            f"  min_samples: {exp.min_samples}\n"
            f"  created_at:  {exp.created_at}\n"
            f"  ended_at:    {exp.ended_at or '(running)'}\n"
            f"  observations: {len(mgr.observations(exp.id))}{winner}"
        )

    if sub == "analyze":
        if len(parts) < 2:
            return _yellow("Usage: /ab analyze <exp_id>")
        a = mgr.analyze(parts[1])
        return _format_analysis(a)

    if sub == "conclude":
        if len(parts) < 2:
            return _yellow("Usage: /ab conclude <exp_id>")
        exp, analysis = mgr.conclude(parts[1])
        if exp is None:
            return _yellow(f"Experiment {parts[1]!r} not found")
        out = [f"{_bold('Conclusion for ' + exp.id)}"]
        if analysis.status == "analyzed" and analysis.winner != "tie":
            out.append(f"  {_green('✓')} winner: {analysis.winner}")
            out.append(f"  status: {exp.status}")
        else:
            out.append(f"  status: {exp.status} (no winner — {analysis.status})")
        out.append("")
        out.append(_format_analysis(analysis))
        return "\n".join(out)

    if sub == "abandon":
        if len(parts) < 2:
            return _yellow("Usage: /ab abandon <exp_id>")
        e = mgr.abandon(parts[1])
        if e is None:
            return _yellow(f"Experiment {parts[1]!r} not found or not running")
        return f"{_green('✓')} Abandoned {e.id}"

    return (
        f"{_yellow('Usage:')} /ab [summary|list|status|analyze|conclude|abandon]\n"
        f"  /ab                       — overall summary\n"
        f"  /ab list                  — list all experiments\n"
        f"  /ab status <exp_id>       — show one experiment\n"
        f"  /ab analyze <exp_id>      — run analysis\n"
        f"  /ab conclude <exp_id>     — conclude + promote winner\n"
        f"  /ab abandon <exp_id>      — mark as abandoned"
    )


def _format_analysis(analysis) -> str:
    """Format an ExperimentAnalysis for CLI display."""
    if analysis.status == "no_data":
        return _yellow("(no observations yet)")
    if analysis.status == "not_found":
        return _yellow("(experiment not found)")
    if analysis.status == "insufficient_samples":
        have_str = ", ".join(f"{k}:{v}" for k, v in analysis.have.items())
        return f"{_yellow('Insufficient samples:')} {have_str}\n  {analysis.details}"
    if analysis.status == "insufficient_variants":
        have_str = ", ".join(f"{k}:{v}" for k, v in analysis.have.items())
        return f"{_yellow('Insufficient variants:')} {have_str}\n  {analysis.details}"
    # analyzed
    lines = [_bold("Analysis:")]
    for vid, r in analysis.results.items():
        avg_rating = f", rating: {r['avg_rating']:.1f}" if r.get("avg_rating") is not None else ""
        lines.append(
            f"  {vid}: n={r['n']:<4d}  "
            f"success: {r['success_rate']:.0%}  "
            f"tokens: {r['avg_tokens']:.0f}  "
            f"duration: {r['avg_duration_ms']:.0f}ms{avg_rating}"
        )
    if analysis.winner == "tie":
        lines.append(_yellow("  Result: tie (within 5% delta)"))
    else:
        lines.append(f"{_green('  Winner:')} {analysis.winner}")
    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="ab",
        description="A/B testing framework (PR-12): list/create/status/analyze/conclude",
        usage="/ab [summary|list|status|analyze|conclude|abandon] [exp_id]",
    )
)
registry._commands["ab"]._handler = _handle_ab


# ── /progress (PR-13) ──────────────────────────────────────────────


async def _handle_progress(args: str, ctx: dict) -> str:
    """PR-13: View or clear the .claude-progress.txt anchor file.

    Subcommands:
        /progress            — show the current progress record
        /progress clear      — delete the progress file (start fresh)
        /progress path       — show the file path
    """
    engine = ctx.get("engine")
    if engine is None or engine.anchor is None:
        return _yellow("Progress anchor is disabled (progress_anchor_enabled=False or no engine)")

    parts = args.strip().split()
    sub = parts[0].lower() if parts else "show"

    if sub == "clear":
        engine.anchor.clear()
        return f"{_green('✓')} Progress file cleared."

    if sub == "path":
        return str(engine.anchor.path)

    # Default: show
    record = engine.anchor.read()
    if record is None or record.is_empty():
        return _yellow("(no progress file)")
    lines = [
        f"{_bold('Progress anchor:')} {engine.anchor.path}",
        f"  current_task: {record.current_task or '(unset)'}",
        f"  current_step: {record.current_step or '(unset)'}",
        f"  next_step:    {record.next_step or '(unset)'}",
        f"  op_hash:      {record.op_hash or '(unset)'}",
    ]
    if record.known_issues:
        lines.append("  known_issues:")
        for issue in record.known_issues:
            lines.append(f"    - {issue}")
    if record.updated_at:
        lines.append(f"  updated_at:   {record.updated_at}")
    if record.extra:
        for k, v in record.extra.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


registry.register(
    SlashCommand(
        name="progress",
        description="View/clear the .claude-progress.txt anchor file (PR-13)",
        usage="/progress [show|clear|path]",
    )
)
registry._commands["progress"]._handler = _handle_progress


# ── /evaluate (PR-09) ──────────────────────────────────────────────


async def _handle_evaluate(args: str, ctx: dict) -> str:
    """PR-09: Run the Evaluator Agent on the last task and write SCORE.md.

    Usage:
        /evaluate                — evaluate using session's audit log
        /evaluate <task>         — override task description
    """
    engine = ctx.get("engine")
    if engine is None:
        return f"{_yellow('⚠ No engine available — cannot evaluate.')}"
    try:
        from pathlib import Path

        from ..agents.evaluator import EvaluatorAgent
        from ..core.audit_log import get_audit_logger
    except Exception as e:
        return f"{_yellow('⚠ Evaluator import failed:')} {e}"
    task = args.strip() or getattr(engine, "_last_task", "(unknown task)")
    try:
        audit_records = get_audit_logger().query(
            agent_id="main",
            limit=1000,
        )
    except Exception:
        audit_records = []
    workspace = getattr(engine, "_workspace", None) or Path.cwd()
    try:
        evaluator = EvaluatorAgent(engine)
        report = await evaluator.evaluate(
            task=task,
            agent_id="main",
            audit_records=audit_records,
            workspace=Path(workspace),
        )
        md_path, json_path = EvaluatorAgent.write_report(report, workspace=Path(workspace))
    except Exception as e:
        return f"{_yellow('⚠ Evaluation failed:')} {e}"
    return (
        f"📊 Task evaluated: {_bold(f'{report.overall_score:.1f}/10')}\n"
        f"   SCORE.md     → {md_path}\n"
        f"   .score.json  → {json_path}"
    )


registry.register(
    SlashCommand(
        name="evaluate",
        description="Run the Evaluator Agent on the last task and write SCORE.md",
        usage="/evaluate [task description]",
    )
)
registry._commands["evaluate"]._handler = _handle_evaluate
