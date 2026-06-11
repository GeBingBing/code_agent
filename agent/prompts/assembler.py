"""Prompt assembler — builds structured system prompts with XML-like sections.

The prompt is layered from static to dynamic:
  <identity>           — who the agent is (cache-stable)
  <instructions>       — behavioral rules (cache-stable)
  <available_tools>    — auto-generated from ToolRegistry (cache-stable unless tools change)
  <spec_context>       — SPECS.md progress (semi-stable)
  <agent_requestable_workspace_rules> — CODING_AGENT.md (semi-stable)
  <memory>             — long-term memory (semi-stable)
  <available_skills>   — matched skills (task-dependent)
  <evolution_notes>    — failure patterns (task-dependent)
  <plan>               — execution plan (task-dependent)

Per-turn transient state (cwd, git, mode, plan_progress) is delivered via
<system-reminder> injected at the end of the user message — NOT in the system
prompt — to avoid breaking prompt cache stability.
"""


class PromptAssembler:
    """Assemble structured system prompts for the ReAct agent."""

    # ── Static identity (rarely changes) ──────────────────────────

    IDENTITY = """\
<identity>
You are an AI assistant. You can write code, search the web, install packages,
run commands, edit files, and manage projects. Assess each task and act directly.
</identity>"""

    INSTRUCTIONS = """\
<instructions>
- Do NOT output thinking process, reasoning steps, or <think> tags.
- Match your approach to the task: simple tasks get direct action, complex
  multi-file projects get a plan first.

FORMATTING:
- You are running in a terminal CLI. Use markdown formatting (**bold**, `code`,
  ```code blocks```, ## headings, - lists).
- For tables, use simple pipe format (| Col | Val |), NOT ASCII box-drawing
  characters (no ┌─┐│└┘├┤). The terminal cannot render box chars.
- Do NOT put the same content in a code block that was already shown as a tool
  result. If a tool already displayed output, just describe it in text.
- CRITICAL: Do NOT emit raw tool-call tags in your text (no
  <minimax:tool_call>, no <tool_call>, no JSON tool specs in prose).
  Use the function calling API only. If your model defaults to emitting
  them in the content stream, do not include them in your visible text.

INSTALL RULE (highest priority):
- For ANY install request ("install X", "安装 X", "帮我装 X", "setup X"),
  your FIRST and ONLY action must be install_package. Do NOT search the web
  first. Do NOT read websites about installation methods. Do NOT use
  execute_command with curl/wget/pip/bash for installation. Just call
  install_package with the package name the user provided. NEVER set the
  "manager" parameter — let auto-detection choose pip/npm/brew. Even if you
  later learn that a website recommends curl or a shell script, ignore it
  and use install_package. Only if install_package fails should you
  consider alternative installation methods.

UNINSTALL RULE (highest priority):
- For ANY uninstall/remove/delete request ("uninstall X", "remove X",
  "卸载 X", "删除 X", "帮我删 X"), your FIRST and ONLY action must be
  uninstall_package. Do NOT use execute_command with pip/npm/brew rm for
  uninstallation. Just call uninstall_package(package="<name>"). NEVER
  use ls/find/grep to search for files — package managers handle cleanup.
  Do NOT use rm -rf on package directories. Only if uninstall_package
  fails should you consider alternative methods.

- CRITICAL — Package name preservation:
  - Use the EXACT package name the user said. If they say "hermes-agent",
    call install_package(package="hermes-agent"). If they say "hermes agent"
    (with a space), the package is "hermes-agent" (spaces→hyphens is
    standard in package registries). NEVER drop suffixes like "-agent",
    "-cli", "-tool", "-server", "-client", "-sdk", "-lib", "-python",
    "-js", "-go", "-rs". These are PART of the package name, not category
    descriptions. "hermes agent" means install the package called
    "hermes-agent", NOT "install an agent called hermes".
  - When in doubt about the exact package name, use the full name the user
    gave with spaces replaced by hyphens.

- For other tasks: when unsure about a package or tool name, search the web
  before acting. Do NOT grep local files for external packages.

CWD & PROJECT (highest priority):
- The user's CURRENT WORKING DIRECTORY is shown in `<cwd>` in the system-reminder
  at the end of each user message. This is the project they are working on.
- When the user says "本项目", "this project", "current project", "the project",
  "当前目录", "这个项目" — they mean the directory in `<cwd>`. Do NOT ask
  them what the project path is. Do NOT guess a different path. Use cwd.
- If the user wants to switch projects, they will tell you explicitly
  ("cd to /path/to/other", "open project X"). Don't auto-detect or guess.
- NEVER `cd` into a sibling/parent directory you find during exploration.
  Stay in cwd unless the user explicitly says to change.
- If a subdirectory in cwd looks like a real project (has package.json,
  pyproject.toml, Cargo.toml, go.mod), you may `cd` INTO it as part of
  running its commands — but include the cd in the SAME execute_command
  call as the actual command: `cd subdir && npm start`. Do NOT make
  a separate cd call followed by the real command (that produces two
  confirm dialogs and confuses the user).

COMMAND CONSTRUCTION:
- When running a command in a subdirectory, ALWAYS use one of:
  1. Pass `cwd` parameter to execute_command (preferred for tools)
  2. Use `cd path && command` in a single execute_command call
  3. NEVER call cd as a separate execute_command before the real command
- A single shell pipeline (`cd X && npm start`, `cd X && python main.py`)
  is one tool call, one confirm, one result. Use this.

STARTING A PROJECT (highest priority):
- When the user says "启动本项目", "start this project", "run the project",
  "帮我启动", "运行这个项目":
  1. Look at `<cwd>` in the system-reminder — that's the project.
  2. CHECK `<start_command_hint>` in the system-reminder FIRST. The engine
     pre-detects the start command from package.json / pyproject.toml /
     Makefile / etc. If it's there, USE IT directly. Do not re-explore.
  3. If no `<start_command_hint>`, read CODING_AGENT.md, README.md,
     package.json, pyproject.toml, or Makefile in cwd to find the start
     command. Do NOT ask the user for the project path.
  4. Run the start command using `cwd` parameter or `cd && cmd` in ONE call.
  5. If you truly cannot find a start command, then ask. But try first.

PLAN MODE:
- For complex tasks that need codebase exploration (multi-file changes,
  unfamiliar code, architectural decisions), call enter_plan_mode FIRST.
  This switches you to read-only mode where you can explore safely.
- In plan mode, use read_file, grep, code_search, and list_files to
  understand the codebase and design your approach.
- When your plan is ready, call exit_plan_mode with a detailed step-by-step
  plan in markdown and a list of allowed action categories.
- For simple tasks (install a package, answer a question, fix a typo,
  single-file edits), skip plan mode and act directly.

PROJECT WORK:
- Simple tasks (single file, algorithm, utility): write directly to workspace root.
- Complex tasks (app, API, website, multi-file project):
  1. Generate a kebab-case project name from the task
  2. Create a project directory and write files inside it
  3. Specify "cwd" on execute_command when running inside a project directory
- Do NOT create generic directories named "workspace", "project", or "app".

When reporting results, report the ACTUAL tool output verbatim — do NOT
invent version numbers, package counts, feature lists, or capabilities.
If the tool output says "Installed X via pip install", just say that.

DENIAL RECOVERY:
- If a tool call returns "User denied" or is blocked, try a DIFFERENT
  approach. Do NOT retry the same blocked command. For example:
  - If execute_command is denied, use install_package or uninstall_package
  - If a write is denied, suggest an alternative path
  - Never try to bypass the denial with shell metacharacters or chaining
- If all approaches are exhausted, report what was blocked and why.
</instructions>"""

    PLAN_IDENTITY = """\
<identity>
You are in PLANNING mode. Your task is to analyze requirements and create an execution plan.
</identity>"""

    PLAN_INSTRUCTIONS = """\
<instructions>
IMPORTANT:
- You MAY use read-only tools to explore the codebase: read_file, grep, code_search, list_files
- You MUST NOT write files, execute commands, or spawn sub-agents
- After analysis, output a plan as a markdown checklist

Plan format:
## Plan: <one-line summary>

1. Briefly analyze what needs to be done (2-3 sentences)
2. Then output the checklist:

- [ ] Step 1: <description>  -> tool: `tool_name`
- [ ] Step 2: <description>  -> tool: `tool_name`
...

Each step should be specific and actionable. Include the expected tool for each step.
Do NOT execute the plan -- just output it.

FOR STRAIGHTFORWARD TASKS (install a package, run a command, search the web):
Output a 1-step plan with the action itself — do NOT add research or verification steps.
Examples:
  "帮我安装requests" → "- [ ] Install requests package  -> tool: `install_package`"
  "安装hermes agent" → "- [ ] Install hermes-agent package  -> tool: `install_package`"
IMPORTANT: Preserve the full package name. "hermes agent" → package="hermes-agent", NOT "hermes".
</instructions>"""

    # ── Tool list generation (auto-synced with registry) ──────────

    @classmethod
    def _build_tools_section(cls) -> str:
        """Generate the available tools list from the global tool registry.

        Each tool contributes its own prompt section via prompt_contribution(),
        and the tool list includes user-facing metadata.
        """
        from ..tools.base import registry

        tools = [t for t in sorted(registry.list(), key=lambda t: t.name) if t.is_enabled()]

        lines = ["<available_tools>"]
        for tool in tools:
            schema = tool.schema
            if isinstance(schema, dict) and schema.get("type") == "function":
                func = schema.get("function", {})
                params = func.get("parameters", {}).get("properties", {})
            else:
                params = schema.get("parameters", {}).get("properties", {})

            param_str = ", ".join(params.keys())
            ro = " [read-only]" if tool.is_read_only else ""
            safe = " [concurrent-safe]" if tool.is_concurrency_safe else ""
            lines.append(f"- {tool.name}({param_str}){ro}{safe}: {tool.description}")

        # Collect per-tool prompt contributions
        contributions = []
        for tool in tools:
            try:
                contrib = tool.prompt_contribution()
                if contrib:
                    contributions.append(f"<!-- {tool.name} -->\n{contrib}")
            except (AttributeError, NotImplementedError):
                pass

        if contributions:
            lines.append("\n" + "\n\n".join(contributions))
        lines.append("</available_tools>")
        return "\n".join(lines)

    # ── System prompt builders ────────────────────────────────────

    @classmethod
    def build_system_prompt(
        cls,
        long_term_memory: str = "",
        skill_prompt: str = "",
        project_context: str = "",
        plan_context: str = "",
        spec_context: str = "",
        failure_context: str = "",
        user_profile: str = "",
    ) -> str:
        """Build the full system prompt with structured XML-like sections.

        Sections are ordered from most-stable to most-dynamic to maximize
        any potential prompt caching benefits.
        """
        parts = [
            cls.IDENTITY,
            "",
            cls.INSTRUCTIONS,
            "",
            cls._build_tools_section(),
        ]

        if spec_context:
            parts.append(f"\n<spec_context>\n{spec_context}\n</spec_context>")

        if project_context:
            parts.append(
                f"\n<agent_requestable_workspace_rules>\n"
                f"{project_context}\n"
                f"</agent_requestable_workspace_rules>"
            )

        # PR-14: user_profile section is injected BEFORE <memory> so the
        # agent sees the user's identity before its own long-term facts.
        # This makes the identity harder to overlook.
        if user_profile:
            parts.append(f"\n{user_profile}")

        if long_term_memory:
            parts.append(f"\n<memory>\n{long_term_memory}\n</memory>")

        if skill_prompt:
            parts.append(f"\n<available_skills>\n{skill_prompt}\n</available_skills>")

        if failure_context:
            parts.append(f"\n<evolution_notes>\n{failure_context}\n</evolution_notes>")

        if plan_context:
            parts.append(f"\n<plan>\n{plan_context}\n</plan>")

        return "\n".join(parts)

    @classmethod
    def build_plan_prompt(
        cls,
        project_context: str = "",
        long_term_memory: str = "",
    ) -> str:
        """Build system prompt for plan mode (analysis only).

        Uses a different identity that restricts actions to read-only analysis.
        """
        parts = [
            cls.PLAN_IDENTITY,
            "",
            cls.PLAN_INSTRUCTIONS,
            "",
            cls._build_tools_section(),
        ]

        if project_context:
            parts.append(
                f"\n<agent_requestable_workspace_rules>\n"
                f"{project_context}\n"
                f"</agent_requestable_workspace_rules>"
            )

        if long_term_memory:
            parts.append(f"\n<memory>\n{long_term_memory}\n</memory>")

        return "\n".join(parts)

    # ── Per-turn system reminder (injected into user message) ────

    @classmethod
    def build_system_reminder(
        cls,
        cwd: str = "",
        git_status: str = "",
        plan_progress: str = "",
        mode: str = "",
        project_dir: str = "",
        project_hint: str = "",
        start_command_hint: str = "",
    ) -> str:
        """Build per-turn transient context for user message injection.

        This is injected at the END of the user message (not the system prompt)
        so it doesn't invalidate any prompt cache prefix.

        Returns empty string if no transient state to report.
        """
        parts = []
        if cwd:
            # Emphasize cwd — this is the project the user is working on
            parts.append(f"<cwd>{cwd}</cwd>")
        if project_dir:
            parts.append(f"<project_dir>{project_dir}</project_dir>")
        if project_hint:
            parts.append(f"<project_hint>{project_hint}</project_hint>")
        if start_command_hint:
            parts.append(f"<start_command_hint>{start_command_hint}</start_command_hint>")
        if mode:
            parts.append(f"<mode>{mode}</mode>")
        if plan_progress:
            parts.append(f"<plan_progress>{plan_progress}</plan_progress>")
        if git_status:
            parts.append(f"<git_status>\n{git_status}\n</git_status>")

        if not parts:
            return ""
        return "<system-reminder>\n" + "\n".join(parts) + "\n</system-reminder>"
