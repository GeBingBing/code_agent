"""ReAct Agent Engine - Phase 1: With memory system"""

import asyncio
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..governance.ab_test import (
    get_ab_test_manager,
)
from ..hooks.ab_test import (
    ABTestApplyHook,
    ABTestRecordObservationHook,
    resolve_ab_user_id,
)
from ..hooks.audit import AuditHook
from ..hooks.dual_review import DualReviewHook
from ..hooks.otel import OtelHook
from ..hooks.progress import ProgressInjectHook, ProgressUpdateHook

# PR-19: Hook implementations live in agent.hooks.* (one class per concern).
# The engine instantiates them in __init__ and registers them with the
# HookRegistry. The 13 inline hook methods that used to live here have
# been moved into those classes. Back-compat properties at the bottom
# of AgentEngine preserve the test API (e.g. e._audit_before_tool).
from ..hooks.ralph import RalphCheckHook
from ..hooks.task_state import TaskStateRecordStepHook
from ..llm.client import LLMClient, Message
from ..mcp.adapter import register_mcp_tools_from_config
from ..observability import get_metrics, get_tracer
from ..prompts.assembler import PromptAssembler
from ..tools import (  # noqa: F401 - triggers tool registration
    audit,
    code_search,
    diagnostics,
    file_ops,
    git_smart,
    git_tool,
    glob_tool,
    grep,
    install,
    lsp_tool,
    notebook_tool,
    plan_mode,
    refactor,
    sandbox,
    shell,
    skill_manager,
    spec_verifier,
    structured_output,
    sub_agent,
    todo_tool,
    web_fetch,
    web_search,
)
from ..tools.base import ToolResult, registry
from ..tools.skill_manager import SkillManager
from .audit_log import get_audit_logger
from .context_builder import ContextBuilder
from .dual_review import (
    DualReviewManager,
)
from .event_bus import EventBus
from .evolution import EvolutionEngine
from .hooks import (
    AFTER_LLM_CALL,
    AFTER_TOOL_EXECUTION,
    BEFORE_LLM_CALL,
    BEFORE_PERCEIVE,
    BEFORE_TOOL_EXECUTION,
    ON_ERROR,
    ON_SESSION_END,
    ON_SESSION_START,
    ON_TOKEN,
    HookRegistry,
)
from .memory import MemoryManager
from .permissions import PermissionManager
from .plan import ExecutionPlan
from .plan_workflow import PlanWorkflow
from .progress_anchor import ProgressAnchor
from .task_state_machine import InvalidStateTransition, TaskState, TaskStateMachine
from .tdd_ralph import RalphSupervisor
from .tdd_state_machine import TDDStateMachine
from .tool_dispatcher import ToolDispatcher

_CODING_AGENT_ROOT = Path(__file__).parent.parent.resolve()
from .config import config as _cfg  # noqa: E402 — depends on _CODING_AGENT_ROOT being set first

WORKSPACE = Path(_cfg.get("workspace") or str(Path.cwd()))

# Keywords for auto-detecting complex multi-file project tasks
_COMPLEX_KEYWORDS = {
    "app",
    "application",
    "website",
    "web",
    "api",
    "rest",
    "graphql",
    "service",
    "server",
    "client",
    "dashboard",
    "admin",
    "blog",
    "todo",
    "shop",
    "store",
    "cms",
    "system",
    "platform",
    "robot",
    "cli",
    "tool",
    "package",
    "library",
    "framework",
    "react",
    "vue",
    "angular",
    "node",
    "django",
    "flask",
    "fastapi",
    "spring",
    "应用",
    "网站",
    "系统",
    "平台",
    "博客",
    "商城",
    "管理后台",
}


def _pick_alternate_model(primary_model: str) -> str:
    """Pick a sibling model name for the alternate dual-review reviewer.

    Goal: even when both reviewers share an LLM client, the *model name*
    differs so the prompt can include "Reviewer A: claude-… / Reviewer B:
    gpt-…" and downstream observers can attribute decisions.

    This is a name-swap, not a real second client. A future PR can wire
    a real cross-provider setup.
    """
    name = (primary_model or "").lower()
    if any(s in name for s in ("claude", "sonnet", "opus", "haiku")):
        return "gpt-4o"
    if any(s in name for s in ("gpt", "o1", "o3", "o4")):
        return "claude-sonnet-4-6"
    if any(s in name for s in ("qwen", "deepseek", "glm", "kimi", "minimax", "doubao")):
        return "gpt-4o"
    return "claude-sonnet-4-6"


def generate_project_name(task: str) -> str:
    """Generate a kebab-case project name from task description.

    Extracts meaningful words from the task, removes common stopwords,
    and joins them with hyphens to create a descriptive project directory name.
    Supports both English and Chinese input.
    """

    from .text_utils import is_stopword as _is_sw

    # Extract English words
    english_words = re.findall(r"[a-zA-Z]+", task.lower())
    english_words = [w for w in english_words if not _is_sw(w) and len(w) > 2]

    if english_words:
        return "-".join(english_words[:4])

    # Fallback: extract Chinese 2-4 char meaningful chunks
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", task)
    chinese_words = []
    for chunk in chinese_chunks:
        if len(chunk) >= 2:
            # Remove stopword characters from chunk
            filtered = "".join(c for c in chunk if not _is_sw(c))
            if filtered and len(filtered) >= 2:
                # Take first 4 chars of filtered chunk
                chinese_words.append(filtered[:4])

    if chinese_words:
        return "-".join(chinese_words[:3])

    return "project"


# ANSI colors - Claude Code inspired palette
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[38;5;196m"  # Red
    GREEN = "\033[38;5;34m"  # Green
    YELLOW = "\033[38;5;226m"  # Yellow
    BLUE = "\033[38;5;75m"  # Blue
    MAGENTA = "\033[38;5;141m"  # Purple
    CYAN = "\033[38;5;39m"  # Cyan
    GRAY = "\033[38;5;240m"  # Gray

    # File status colors
    ADDED = "\033[38;5;82m"  # Bright green for A
    MODIFIED = "\033[38;5;214m"  # Orange for M
    DELETED = "\033[38;5;196m"  # Red for D


def print_step(step: int, total: int, label: str, verbose: bool = True):
    """Print a progress step indicator - Claude Code style."""
    if not verbose:
        return
    print(f"{Colors.DIM}[{step}/{total}]{Colors.RESET} {label}", flush=True)


def print_tool_call(tool_name: str, args: dict, verbose: bool = True):
    """Print a tool call - flat list style, not tree."""
    if not verbose or not tool_name:
        return

    # Format based on tool type
    if tool_name == "write_file":
        path = args.get("path", "unknown")
        print(f"{Colors.GREEN}+{Colors.RESET} {Colors.BOLD}{path}{Colors.RESET}", flush=True)
    elif tool_name == "read_file":
        path = args.get("path", "unknown")
        print(f"{Colors.CYAN}@{Colors.RESET} {path}", flush=True)
    elif tool_name == "edit_file":
        path = args.get("path", "unknown")
        print(f"{Colors.YELLOW}~{Colors.RESET} {Colors.BOLD}{path}{Colors.RESET}", flush=True)
    elif tool_name == "execute_command":
        cmd = args.get("command", "")[:60]
        print(f"{Colors.MAGENTA}>{Colors.RESET} {cmd}", flush=True)
    else:
        # Generic tool call
        args_str = ", ".join(f"{k}={repr(v)[:30]}" for k, v in list(args.items())[:2])
        print(
            f"{Colors.DIM}→{Colors.RESET} {Colors.CYAN}{tool_name}{Colors.RESET}({args_str})",
            flush=True,
        )


def print_tool_result(success: bool, content: str, error: str = None, verbose: bool = True):
    """Print a tool result - minimal, just status."""
    if not verbose:
        return
    if success:
        # Show brief preview for file operations
        if content and len(content) < 80:
            print(f"{Colors.DIM}  {content}{Colors.RESET}", flush=True)
    else:
        pass  # Errors are shown inline with the tool call, don't duplicate


def print_thinking(verbose: bool = True):
    """Print a thinking indicator - subtle."""
    if not verbose:
        return
    print(f"{Colors.DIM}thinking...{Colors.RESET}", flush=True)


def _apply_ansi_styles(text: str) -> str:
    """Apply ANSI colors to Markdown-style bold and italic."""

    # Bold: **text** -> yellow bold
    text = re.sub(r"\*\*(.+?)\*\*", f"{Colors.YELLOW}{Colors.BOLD}\\1{Colors.RESET}", text)
    # Italic: *text* -> cyan
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", f"{Colors.CYAN}\\1{Colors.RESET}", text)
    # Inline code: `code` -> green
    text = re.sub(r"`([^`]+)`", f"{Colors.GREEN}\\1{Colors.RESET}", text)

    return text


def _format_markdown(text: str) -> str:
    """Format text with basic Markdown styling."""
    lines = text.split("\n")
    formatted = []
    in_code_block = False

    for line in lines:
        # Code block detection
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            formatted.append(line)
            continue

        if in_code_block:
            formatted.append(f"  {line}")
            continue

        # Headers
        if line.startswith("# "):
            header_text = line[2:]
            formatted.append(f"\n{Colors.CYAN}{Colors.BOLD}{header_text}{Colors.RESET}")
            continue

        # Horizontal rule
        if line.strip() == "---":
            formatted.append(f"{Colors.DIM}---{Colors.RESET}")
            continue

        # Apply bold/italic styling
        line = _apply_ansi_styles(line)

        # Bullet points
        if line.strip().startswith(("• ", "- ", "* ")):
            formatted.append(f"  {line}")
            continue

        # Numbered lists
        if line.strip()[0:2].rstrip(".").isdigit() and ". " in line[:4]:
            formatted.append(f"  {line}")
            continue

        # Empty lines
        if not line.strip():
            formatted.append("")
            continue

        formatted.append(line)

    return "\n".join(formatted)


def print_done(final: str):
    """Print the final result - immediate output (streaming handles real-time display)."""
    pass  # No longer needed - streaming handles output in real-time


def _load_config_file():
    """Load config from ~/.coding-agent/config.json if it exists."""
    config_path = Path.home() / ".coding-agent" / "config.json"
    if config_path.exists():
        try:
            import json

            with open(config_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


@dataclass
class AgentConfig:
    model: Optional[str] = None
    provider: Optional[str] = None
    mode: Optional[str] = None  # plan/default/auto/bypass
    max_steps: int = 200
    max_tokens: int = 15000
    verbose: bool = True
    max_tool_retries: int = 1
    auto_evolve: bool = False
    mcp_enabled: bool = False
    mcp_config_path: str = ""
    custom_system_prompt: str = ""
    confirm_handler: Optional[Callable[[str, str, dict], Awaitable[str]]] = None
    tdd_mode: str = "guided"  # PR-02: strict | guided | off
    codmap_enabled: bool = True  # PR-05: inject repo map before LLM calls
    spec_ac_inject: bool = True  # PR-06: inject unfinished ACs into LLM context
    audit_enabled: bool = True  # PR-08: record tool calls/results to audit log
    otel_enabled: bool = True  # PR-10: emit OTel spans/metrics for tool+LLM calls
    enable_dual_review: bool = True  # PR-11: dual-agent review for high-risk tools
    dual_review_model: str = ""  # PR-11: override the alternate reviewer model (empty = auto)
    # P14-2: when True, secondary reviewer uses a SECOND, different LLM client
    # (cross-provider — e.g. GPT primary, DashScope secondary). When False
    # (default), both reviewers share self.llm (single-client mode, backward
    # compatible). D3 增强: defaults to False so single-provider users are
    # unaffected; users opt in via config or DUAL_REVIEW_PROVIDER env.
    dual_review_strict_cross_provider: bool = False
    ab_test_enabled: bool = True  # PR-12: A/B test engine integration
    ab_user_id: str = ""  # PR-12: stable user identifier for bucketing (empty = auto-derive)
    progress_anchor_enabled: bool = True  # PR-13: write/read .claude-progress.txt
    progress_workspace: str = ""  # PR-13: override workspace (default = WORKSPACE)
    # PR-14: user profile (root-cause fix for session amnesia)
    user_profile_enabled: bool = True  # Master switch: load ~/.coding-agent/user_profile.json
    auto_remember_user_facts: bool = True  # Auto-extract "I'm X" / "我是 X" from user msgs
    memory_pinned_max: int = 200  # Cap for pinned memory.md entries
    # PR-15: LLM-based extractors (replacing hard-coded regex lists)
    intent_use_llm: bool = True  # Use LLM for IntentClassifier
    fact_extraction_use_llm: bool = True  # Use LLM for FactExtractor

    def __post_init__(self):
        """Fill in model/provider/mode from config if not explicitly set."""
        if self.model is None:
            self.model = _cfg.get("model")
        if self.provider is None:
            self.provider = _cfg.get("provider")
        if self.mode is None:
            self.mode = _cfg.get("mode")
        # PR-14: pull PR-14 flags from global config (env > config.json > default)
        if self.user_profile_enabled is None:
            self.user_profile_enabled = _cfg.get("user_profile_enabled", True)
        if self.auto_remember_user_facts is None:
            self.auto_remember_user_facts = _cfg.get("auto_remember_user_facts", True)
        if self.memory_pinned_max is None:
            self.memory_pinned_max = int(_cfg.get("memory_pinned_max", 200))
        # PR-15: pull PR-15 flags from global config
        if self.intent_use_llm is None:
            self.intent_use_llm = _cfg.get("intent_use_llm", True)
        if self.fact_extraction_use_llm is None:
            self.fact_extraction_use_llm = _cfg.get("fact_extraction_use_llm", True)


# Structured JSON logger
_log_dir = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent" / "logs"))
_log_dir.mkdir(parents=True, exist_ok=True)
_agent_log_file = _log_dir / "agent.jsonl"


def _log_event(trace_id: str, event: str, **kwargs):
    """Write a structured JSON log entry."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "trace_id": trace_id,
        "event": event,
        **kwargs,
    }
    try:
        with open(_agent_log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Don't fail on logging errors


class AgentEngine:
    """ReAct agent with tool calling, memory, skills, and permissions."""

    def __init__(self, config: Optional[AgentConfig] = None):
        self.config = config or AgentConfig()
        self.trace_id = str(uuid.uuid4())[:8]  # Unique per run
        if self.config.model != "mock" and self.config.provider != "mock":
            self.llm = LLMClient(
                model=self.config.model,
                provider=self.config.provider,
            )
        else:
            self.llm = None
        self.memory = MemoryManager(max_tokens=self.config.max_tokens)
        # PR-14: propagate the pinned-max cap to MemoryManager
        if getattr(self.config, "memory_pinned_max", None):
            try:
                self.memory._PINNED_MAX = int(self.config.memory_pinned_max)
            except (TypeError, ValueError):
                pass
        self.skills = SkillManager()
        self.permissions = PermissionManager(self.config.mode)
        self._consecutive_failures = {}  # tool_name -> failure_count for circuit breaker
        self.current_project_dir: Optional[str] = (
            None  # Detected project directory for multi-file tasks
        )
        self.evolution = EvolutionEngine(enabled=self.config.auto_evolve)
        self._mcp_manager = None  # Initialized lazily on first run
        self._mcp_initialized = False
        self._current_plan = None  # ExecutionPlan reference for progress tracking
        self._running_tasks: set = set()  # Track spawned asyncio tasks for cleanup
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._last_usage_estimated = False  # True if last turn's usage was estimated
        self._context_window = 128000  # default, configurable
        self._compact_threshold = 0.75

        # PR-01: EventBus (observers) + HookRegistry (lifecycle extension points)
        self.event_bus = EventBus()
        self.hooks = HookRegistry()

        # PR-14: User profile — root-cause fix for session amnesia.
        # Loaded once at engine construction; user_profile_enabled flag
        # controls whether the default ON_SESSION_START handler is registered.
        from .hooks import ON_SESSION_START
        from .hooks_session import load_user_profile_on_start
        from .user_profile import UserProfile

        self.user_profile: Optional[UserProfile] = None
        if self.config.user_profile_enabled:
            try:
                self.user_profile = UserProfile.load()
            except Exception:
                self.user_profile = UserProfile()
            # Register default handler so ON_SESSION_START injects profile
            self.hooks.register(ON_SESSION_START, load_user_profile_on_start)

        # PR-02: TDD state machine + Ralph supervisor
        self.tdd_state_machine = TDDStateMachine(mode=self.config.tdd_mode)
        self.ralph = RalphSupervisor(self.tdd_state_machine)
        self.ralph_hook = RalphCheckHook(self.ralph)
        # Register Ralph check on BEFORE_TOOL_EXECUTION
        if self.config.tdd_mode in ("strict", "guided"):
            self.hooks.register(BEFORE_TOOL_EXECUTION, self.ralph_hook)

        # PR-03: Task state machine + persistence
        self.task_state_machine = TaskStateMachine()
        self.task_state_hook = TaskStateRecordStepHook(self.task_state_machine)
        # Record completed steps automatically via AFTER_TOOL_EXECUTION
        self.hooks.register(AFTER_TOOL_EXECUTION, self.task_state_hook)

        # PR-20: ContextBuilder owns prompt-context sources (codmap, project,
        # spec, ACs) and the system-prompt assembly. Engine keeps thin aliases
        # for tests/back-compat (`self._codmap`, `self.spec_document`, etc.).
        self.context_builder = ContextBuilder(
            config=self.config,
            memory=self.memory,
            user_profile=getattr(self, "user_profile", None),
            workspace=WORKSPACE,
        )
        if self.context_builder.codmap_active:
            self.hooks.register(BEFORE_LLM_CALL, self.context_builder.inject_codmap)
        if self.context_builder.spec_ac_inject_active:
            self.hooks.register(BEFORE_LLM_CALL, self.context_builder.inject_spec_acs)

        # PR-21: PlanWorkflow owns plan-then-execute orchestration. Engine
        # delegates `run_plan` / `run_execute` to it.
        self.plan_workflow = PlanWorkflow(
            llm=self.llm,
            permissions=self.permissions,
            memory=self.memory,
            context_builder=self.context_builder,
            skills=self.skills,
            config=self.config,
            trace_id=self.trace_id,
            get_current_plan=lambda: self._current_plan,
            set_current_plan=lambda p: setattr(self, "_current_plan", p),
            get_env_context=self._get_env_context,
            execute_tool=self._execute_tool,
        )

        # PR-23: ToolDispatcher owns the 11-stage tool-call pipeline.
        # Engine delegates `_execute_tool` to it.
        self.tool_dispatcher = ToolDispatcher(
            hooks=self.hooks,
            event_bus=self.event_bus,
            permissions=self.permissions,
            memory=self.memory,
            trace_id=self.trace_id,
            workspace=WORKSPACE,
            get_current_project_dir=lambda: getattr(self, "current_project_dir", None),
            set_current_project_dir=lambda v: setattr(self, "current_project_dir", v),
            get_pre_plan_mode=lambda: getattr(self, "_pre_plan_mode", None),
            set_pre_plan_mode=lambda v: setattr(self, "_pre_plan_mode", v),
            get_confirm_handler=lambda: self.config.confirm_handler,
            log_event=_log_event,
        )

        # PR-08: Append-only audit log. Records every tool_call + tool_result
        # via hooks so the trail is automatic — no per-tool wiring required.
        # PR-19: always instantiate AuditHook (it handles audit=None → no-op);
        # only register with the registry if audit is enabled.
        self.audit = None
        self.audit_hook = AuditHook(None, self.trace_id)
        if getattr(self.config, "audit_enabled", True):
            try:
                self.audit = get_audit_logger()
                self.audit_hook = AuditHook(self.audit, self.trace_id)
                self.hooks.register(BEFORE_TOOL_EXECUTION, self.audit_hook.before_tool)
                self.hooks.register(AFTER_TOOL_EXECUTION, self.audit_hook.after_tool)
            except Exception:
                # Never let audit init fail the engine
                self.audit = None

        # PR-10: OpenTelemetry-compatible tracing + metrics. Uses no-op shims
        # when the OTel SDK isn't installed — zero cost. Hook into tool +
        # LLM call boundaries so observability is automatic.
        # PR-19: same pattern — always instantiate OtelHook, register only
        # when tracer is available.
        self.tracer = None
        self.metrics = None
        self.otel_hook = OtelHook(
            None, None, self.trace_id, self.config.model, self.config.provider
        )
        if getattr(self.config, "otel_enabled", True):
            try:
                self.tracer = get_tracer()
                self.metrics = get_metrics()
                self.otel_hook = OtelHook(
                    self.tracer,
                    self.metrics,
                    self.trace_id,
                    self.config.model,
                    self.config.provider,
                )
                self.hooks.register(BEFORE_TOOL_EXECUTION, self.otel_hook.before_tool)
                self.hooks.register(AFTER_TOOL_EXECUTION, self.otel_hook.after_tool)
                self.hooks.register(BEFORE_LLM_CALL, self.otel_hook.before_llm)
                self.hooks.register(AFTER_LLM_CALL, self.otel_hook.after_llm)
            except Exception:
                self.tracer = None
                self.metrics = None

        # PR-11: Dual-agent review for high-risk tool calls. Wires into
        # BEFORE_TOOL_EXECUTION: each high-risk tool call is reviewed by
        # two independent agents in parallel; results are aggregated and
        # any rejection blocks the call. The hook is fire-and-forget —
        # non high-risk tools pass through untouched.
        # PR-19: extracted to DualReviewHook. Uses getters so tests
        # (and runtime config) can mutate `engine.dual_review` /
        # `engine.audit` and have the hook see the new value.
        self.dual_review = None
        self.dual_review_hook = DualReviewHook(
            get_dual_review=lambda: self.dual_review,
            get_audit=lambda: self.audit,
            get_trace_id=lambda: self.trace_id,
        )
        if getattr(self.config, "enable_dual_review", True):
            try:
                self.dual_review = self._build_dual_review_manager()
                if self.dual_review is not None:
                    self.hooks.register(BEFORE_TOOL_EXECUTION, self.dual_review_hook)
                    # P12-3: register a sibling hook that transitions the task
                    # state machine to REVIEW when dual review fires. The
                    # dual review hook itself never touches state; this keeps
                    # the cross-cutting concern isolated.
                    self.hooks.register(BEFORE_TOOL_EXECUTION, self._review_state_transition_hook)
            except Exception:
                # Never let dual-review init break the engine
                self.dual_review = None

        # PR-12: A/B test framework. Wires into BEFORE_LLM_CALL (apply
        # active variants to the system prompt) and ON_SESSION_END
        # (record observations per (experiment, variant)). The manager
        # itself is lazy: it loads from disk on first access and is
        # shared across all engines in the same process.
        # PR-19: extracted to ABTestApplyHook + ABTestRecordObservationHook.
        self.ab_test = None
        self.ab_apply_hook: Optional[ABTestApplyHook] = None
        self.ab_record_hook: Optional[ABTestRecordObservationHook] = None
        if getattr(self.config, "ab_test_enabled", True):
            try:
                self.ab_test = get_ab_test_manager()
                # Resolve the stable user_id used for bucketing
                self._ab_user_id = resolve_ab_user_id(self.config, WORKSPACE)
                # Per-task tracking — _ab_task_start_ts is set in run_stream
                self._ab_task_start_ts: Optional[float] = None
                self._ab_last_task: str = ""
                self._ab_experiments_in_flight: list = []
                self.ab_apply_hook = ABTestApplyHook(self.ab_test, self._ab_user_id)
                self.ab_record_hook = ABTestRecordObservationHook(
                    self.ab_test,
                    self._ab_user_id,
                    get_task_start_ts=lambda: self._ab_task_start_ts,
                    get_last_task=lambda: self._ab_last_task,
                    get_total_input_tokens=lambda: self._total_input_tokens,
                    get_total_output_tokens=lambda: self._total_output_tokens,
                )
                self.hooks.register(BEFORE_LLM_CALL, self.ab_apply_hook)
                self.hooks.register(ON_SESSION_END, self.ab_record_hook)
            except Exception:
                self.ab_test = None

        # PR-13: Progress anchor file. Writes .claude-progress.txt
        # after every tool call so the next session (or the same
        # session after a crash) can resume from the last good step.
        # Hooks: BEFORE_LLM_CALL injects the current state as a
        # system-reminder; AFTER_TOOL_EXECUTION updates the file.
        # PR-19: extracted to ProgressInjectHook + ProgressUpdateHook.
        self.anchor = None
        self._resumed_from_anchor = False
        self.progress_inject_hook: Optional[ProgressInjectHook] = None
        self.progress_update_hook: Optional[ProgressUpdateHook] = None
        if getattr(self.config, "progress_anchor_enabled", True):
            try:
                ws = None
                ws_override = getattr(self.config, "progress_workspace", "") or ""
                if ws_override:
                    ws = Path(ws_override)
                else:
                    ws = WORKSPACE
                self.anchor = ProgressAnchor(workspace=ws)
                self.progress_inject_hook = ProgressInjectHook(self.anchor)
                self.progress_update_hook = ProgressUpdateHook(
                    self.anchor,
                    self.config.max_steps,
                    get_current_plan=lambda: self._current_plan,
                    get_last_task=lambda: self._ab_last_task,
                )
                self.hooks.register(BEFORE_LLM_CALL, self.progress_inject_hook)
                self.hooks.register(AFTER_TOOL_EXECUTION, self.progress_update_hook)
            except Exception:
                self.anchor = None

        # Load project context from CODING_AGENT.md at workspace root
        self.project_context = self.context_builder.project_context
        # Load spec context from SPECS.md (delegated via ContextBuilder)
        self.spec_context = self.context_builder.spec_context
        # PR-20: keep back-compat attr aliases for tests / external callers
        self._codmap = self.context_builder._codmap
        self.spec_document = self.context_builder.spec_document
        _log_event(
            self.trace_id, "engine_init", model=self.config.model, provider=self.config.provider
        )

    # ── PR-20: ContextBuilder delegation shims (back-compat) ────────
    # ── Public read-only API ──────────────────────────────────────
    # Token accumulators + estimated-flag. Exposed so the CLI / TUI /
    # /status command can show real LLM-reported usage without
    # reaching into private attributes.

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    @property
    def last_usage_estimated(self) -> bool:
        """True if the most recent turn's usage was an estimate
        (LLM didn't report usage — e.g. Ollama, some proxies)."""
        return self._last_usage_estimated

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """CJK-aware rough token estimate. Used as a fallback when the
        LLM provider doesn't surface `usage` in the stream.

        Heuristic: each CJK ideograph ≈ 1 token (they tokenize that
        way in BPE); other chars ≈ 1 token per 4 (GPT/Claude average).
        """
        if not text:
            return 0
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        non_cjk = len(text) - cjk
        return cjk + non_cjk // 4

    # Engine no longer owns prompt-context logic — ContextBuilder does.
    # These one-liners preserve the public method surface so tests and
    # any external callers continue to work without change.

    async def _inject_codmap(self, payload):
        return await self.context_builder.inject_codmap(payload)

    async def _inject_spec_acs(self, payload):
        return await self.context_builder.inject_spec_acs(payload)

    def _load_project_context(self) -> str:
        return self.context_builder.project_context

    def _get_system_prompt(self, task="", skill_prompt="", plan_context="", failure_context=""):
        return self.context_builder.get_system_prompt(
            task=task,
            skill_prompt=skill_prompt,
            plan_context=plan_context,
            failure_context=failure_context,
        )

    async def _ensure_mcp_initialized(self):
        """Lazily initialize MCP tool servers on first run."""
        if self._mcp_initialized or not self.config.mcp_enabled:
            return
        self._mcp_initialized = True
        config_path = self.config.mcp_config_path or None
        self._mcp_manager = await register_mcp_tools_from_config(config_path)

    def _get_env_context(self) -> dict:
        """Gather transient environment state for system-reminder injection."""
        import subprocess

        cwd = str(WORKSPACE)

        # Git branch and status summary
        git_status = ""
        try:
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                cwd=WORKSPACE,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if branch:
                status = subprocess.check_output(
                    ["git", "status", "--short"],
                    cwd=WORKSPACE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                git_status = f"On branch {branch}"
                if status:
                    git_status += f"\n{status[:500]}"
        except Exception:
            pass

        # Plan progress
        plan_progress = ""
        if self._current_plan:
            plan_progress = self._current_plan.progress()

        # Project markers in cwd — helps the LLM know what kind of project
        # this is (Python/Node/Go/etc.) and find the start command.
        project_hint = ""
        start_command_hint = ""
        try:
            markers = {
                "package.json": "node",
                "pyproject.toml": "python",
                "requirements.txt": "python",
                "Cargo.toml": "rust",
                "go.mod": "go",
                "pom.xml": "java",
                "build.gradle": "java",
                "Makefile": "make",
                "README.md": "readme",
                "CODING_AGENT.md": "coding-agent-instructions",
            }
            found = []
            for fname, kind in markers.items():
                if (WORKSPACE / fname).exists():
                    found.append(f"{fname}({kind})")
            if found:
                project_hint = f"project markers: {', '.join(found)}"

            # ── Pre-detect the start command ──
            # This saves the LLM from having to figure it out — common
            # commands for "启动本项目" are pre-computed here.
            start_command_hint = self._detect_start_command(WORKSPACE)
        except Exception:
            pass

        return {
            "cwd": cwd,
            "git_status": git_status,
            "plan_progress": plan_progress,
            "mode": self.config.mode,
            "project_dir": self.current_project_dir or "",
            "project_hint": project_hint,
            "start_command_hint": start_command_hint,
        }

    @staticmethod
    def _detect_start_command(workspace: "Path") -> str:
        """Pre-detect the start command for the project in cwd.

        Saves the LLM from having to glob through package.json / pyproject.toml
        when the user says "启动本项目" / "start this project". Returns a
        one-line command hint (or empty string if undetermined).
        """
        import json

        try:
            # ── Node.js: package.json has a "scripts.start" field ──
            pkg_json = workspace / "package.json"
            if pkg_json.exists():
                try:
                    pkg = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
                    scripts = pkg.get("scripts", {})
                    # Prefer "start", fall back to "dev"
                    for key in ("start", "dev"):
                        if key in scripts:
                            cmd = scripts[key]
                            pm = "npm" if (workspace / "package-lock.json").exists() else "npm"
                            return f"npm run {key}  # = `{cmd[:60]}`"
                    # Has package.json but no start script — list scripts
                    if scripts:
                        keys = list(scripts.keys())[:5]
                        return f"package.json scripts: {', '.join(keys)}"
                except Exception:
                    pass

            # ── Python: pyproject.toml has [project.scripts] or [tool.poetry.scripts] ──
            pyproject = workspace / "pyproject.toml"
            if pyproject.exists():
                txt = pyproject.read_text(encoding="utf-8", errors="replace")
                # Check for [project.scripts] block
                if "[project.scripts]" in txt or "[tool.poetry.scripts]" in txt:
                    return "python -m <module>  # see [project.scripts] in pyproject.toml"
                # Check for [tool.poetry] name + a 'main' module
                if "[tool.poetry]" in txt and "name =" in txt:
                    return "python -m <module>  # poetry project — see pyproject.toml"
                # Has pyproject.toml — try main.py / app.py
                for entry in ("main.py", "app.py", "manage.py", "run.py", "server.py"):
                    if (workspace / entry).exists():
                        return f"python {entry}"

            # ── Makefile ──
            makefile = workspace / "Makefile"
            if makefile.exists():
                txt = makefile.read_text(encoding="utf-8", errors="replace")
                # Look for a "run:" or "start:" or first non-.PHONY target
                for line in txt.splitlines():
                    line = line.rstrip()
                    if line and not line.startswith(("\t", ".", "#", " ", "$")) and ":" in line:
                        target = line.split(":", 1)[0].strip()
                        if target and not target.startswith("."):
                            return f"make {target}"
                return "make  # see Makefile for targets"

            # ── Go: go.mod + main.go ──
            if (workspace / "go.mod").exists():
                return "go run ."

            # ── Rust: Cargo.toml ──
            if (workspace / "Cargo.toml").exists():
                return "cargo run"

        except Exception:
            pass
        return ""

    @staticmethod
    def _format_confirm_message(tool_name: str, args: dict) -> str:
        """Format a confirmation message for display in CLI — Claude Code style."""
        if tool_name == "execute_command":
            cmd = args.get("command", "")
            return f"Run command: {cmd}"
        elif tool_name == "write_file":
            path = args.get("path", "unknown")
            return f"Write to: {path}"
        elif tool_name == "delete_file":
            path = args.get("path", "unknown")
            return f"Delete: {path}"
        elif tool_name == "install_package":
            pkg = args.get("package", "")
            mgr = args.get("manager", "auto")
            return f"Install {pkg}" + (f" via {mgr}" if mgr != "auto" else "")
        elif tool_name == "apply_diff":
            path = args.get("path", "unknown")
            return f"Apply diff to: {path}"
        else:
            key = next(iter(args)) if args else ""
            val = str(args.get(key, ""))[:60]
            return f"{tool_name}: {key}={val}" if key else tool_name

    @staticmethod
    def _partition_tool_calls(tool_calls: list) -> tuple:
        """PR-23: thin shim — delegates to ToolDispatcher.partition()."""
        return ToolDispatcher.partition(tool_calls)

    async def _execute_tool(
        self, func_name: str, args: dict, tc_id: str, func_args_raw: str
    ) -> ToolResult:
        """PR-23: thin shim — delegates to ToolDispatcher.execute()."""
        return await self.tool_dispatcher.execute(
            func_name,
            args,
            tc_id,
            func_args_raw,
        )

    async def _ralph_check_hook(self, payload):  # back-compat shim (see below)
        """PR-19: This method is now a back-compat shim.

        See `RalphCheckHook` in agent/hooks/ralph.py for the implementation.
        The shim returns the bound method of the hook instance so that
        existing test calls (e.g. `e._ralph_check_hook(payload)`) still work.
        """
        return await self.ralph_hook(payload)

    async def _task_state_record_step(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see TaskStateRecordStepHook."""
        return await self.task_state_hook(payload)

    def _task_state_transition(self, new_state: "TaskState", **kwargs) -> None:
        """P12-3: Wire task state transitions into the engine's control flow.

        Swallows InvalidStateTransition so a state machine error never breaks
        the agent — the state machine is a recovery aid, not a hard gate.
        """
        if not getattr(self, "task_state_machine", None):
            return
        try:
            self.task_state_machine.transition(new_state, **kwargs)
        except InvalidStateTransition:
            # Allow re-entry / illegal transition silently — the FSM is an
            # observability/recovery tool, not a control-flow gate.
            pass
        except Exception:
            pass

    async def _audit_before_tool(self, payload):  # back-compat shim
        """PR-08: Audit log — record every tool_call with hashed args.

        Hook on BEFORE_TOOL_EXECUTION. Captures wall-clock start time on the
        payload so the after-hook can compute duration.
        """
        if self.audit is None or not isinstance(payload, dict):
            return payload
        # Mark start time for the after-hook to compute duration
        import time as _time

        payload["_audit_start_ts"] = _time.time()
        tool_name = payload.get("tool", "")
        args = payload.get("args", {})
        try:
            self.audit.log(
                {
                    "session_id": self.trace_id,
                    "agent_id": "main",
                    "action": "tool_call",
                    "tool": tool_name,
                    "args": args,
                }
            )
        except Exception:
            pass  # Audit must never break tool execution
        return payload

    async def _audit_after_tool(self, payload):
        """PR-08: Audit log — record tool_result + duration.

        Hook on AFTER_TOOL_EXECUTION.
        """
        if self.audit is None or not isinstance(payload, dict):
            return payload
        import time as _time

        start_ts = payload.get("_audit_start_ts")
        duration_ms = None
        if isinstance(start_ts, (int, float)):
            duration_ms = (_time.time() - start_ts) * 1000.0
        tool_name = payload.get("tool", "")
        result = payload.get("result")
        error = payload.get("error")
        try:
            self.audit.log(
                {
                    "session_id": self.trace_id,
                    "agent_id": "main",
                    "action": "tool_result",
                    "tool": tool_name,
                    "result": result if result is not None else None,
                    "duration_ms": duration_ms,
                    "error": str(error) if error else None,
                }
            )
        except Exception:
            pass
        return payload

    # ── PR-10: OpenTelemetry hooks ───────────────────────────────

    async def _otel_before_tool(self, payload):
        """Start a tool.execute span. Stash span + start_ts on the payload."""
        if self.tracer is None or not isinstance(payload, dict):
            return payload
        import time as _time

        try:
            span = self.tracer.start_span(
                "tool.execute",
                attributes={
                    "tool.name": payload.get("tool", ""),
                    "agent.session_id": self.trace_id,
                },
            )
            payload["_otel_span"] = span
            payload["_otel_start_ts"] = _time.time()
        except Exception:
            pass
        return payload

    async def _otel_after_tool(self, payload):
        """Close the tool.execute span + record duration / counter / failure."""
        if self.tracer is None or not isinstance(payload, dict):
            return payload
        import time as _time

        span = payload.get("_otel_span")
        start = payload.get("_otel_start_ts")
        result = payload.get("result")
        error = payload.get("error")
        success = error is None and (
            getattr(result, "success", True) if result is not None else True
        )
        duration_ms = 0.0
        if isinstance(start, (int, float)):
            duration_ms = (_time.time() - start) * 1000.0
        if span is not None:
            try:
                span.set_attribute("tool.duration_ms", duration_ms)
                span.set_attribute("tool.success", success)
                if error:
                    span.set_attribute("tool.error", str(error)[:200])
                span.end()
            except Exception:
                pass
        if self.metrics is not None:
            try:
                self.metrics.record_tool_call(
                    tool=payload.get("tool", "unknown"),
                    duration_ms=duration_ms,
                    success=success,
                )
            except Exception:
                pass
        return payload

    async def _otel_before_llm(self, payload):
        """Start a llm.call span. Engine's BEFORE_LLM_CALL payload is the
        messages list; we just count it for the attribute."""
        if self.tracer is None or not isinstance(payload, dict):
            return payload
        import time as _time

        messages = payload.get("messages")
        try:
            span = self.tracer.start_span(
                "llm.call",
                attributes={
                    "llm.model": self.config.model or "",
                    "llm.provider": self.config.provider or "",
                    "llm.message_count": len(messages) if isinstance(messages, list) else 0,
                    "agent.session_id": self.trace_id,
                },
            )
            payload["_otel_llm_span"] = span
            payload["_otel_llm_start"] = _time.time()
        except Exception:
            pass
        return payload

    async def _otel_after_llm(self, payload):
        """Close llm.call span + record token usage if present."""
        if self.tracer is None or not isinstance(payload, dict):
            return payload
        import time as _time

        span = payload.get("_otel_llm_span")
        start = payload.get("_otel_llm_start")
        if span is not None:
            try:
                if isinstance(start, (int, float)):
                    span.set_attribute("llm.duration_ms", (_time.time() - start) * 1000.0)
                usage = payload.get("usage") or {}
                if isinstance(usage, dict):
                    if "input_tokens" in usage:
                        span.set_attribute("llm.input_tokens", int(usage["input_tokens"]))
                    if "output_tokens" in usage:
                        span.set_attribute("llm.output_tokens", int(usage["output_tokens"]))
                span.end()
            except Exception:
                pass
        # Metrics: record token counts when available
        if self.metrics is not None:
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                try:
                    self.metrics.record_tokens(
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        model=self.config.model or "unknown",
                    )
                except Exception:
                    pass
        return payload

    # ── PR-11: Dual-agent review ──────────────────────────────────

    def _build_dual_review_manager(self) -> Optional[DualReviewManager]:
        """Create a DualReviewManager using self.llm as primary.

        Secondary reviewer behavior (P14-2):
        - ``dual_review_strict_cross_provider=False`` (default): secondary uses
          the same client as primary (backward compatible — single-provider
          users are unaffected). The model name differs via
          ``_pick_alternate_model``, so the cross-judging story is plausible
          when the underlying LLM honors model names.
        - ``dual_review_strict_cross_provider=True``: secondary uses a SECOND
          LLMClient constructed via ``create_alternate_provider_client()``,
          which picks a different-family provider based on env API keys
          (e.g. OpenAI primary → DashScope secondary). When only one provider
          is available in the environment, secondary silently falls back to
          the primary client (no error — single-provider users are not
          punished).

        Tests can inject their own DualReviewManager via ``engine.dual_review``.
        """
        primary_chat = None
        if self.llm is not None:
            llm = self.llm

            async def _chat(messages, stream: bool = False):
                # Match LLMClient.chat signature: messages, stream, **kwargs
                # The DualReviewManager passes a list[Message] and stream=False.
                resp, _meta = await llm.chat(messages, stream=stream)
                return resp, _meta

            primary_chat = _chat
        # Pick an alternate model. Default to a sibling of the primary.
        primary_model = self.config.model or "primary"
        alternate = self.config.dual_review_model or _pick_alternate_model(primary_model)

        # P14-2: optionally wire a SECOND, different-family LLM client.
        secondary_chat = primary_chat  # default: same client (backward compat)
        if (
            getattr(self.config, "dual_review_strict_cross_provider", False)
            and self.llm is not None
        ):
            try:
                from ..llm.client import create_alternate_provider_client

                alt_client = create_alternate_provider_client(self.llm)
            except Exception:
                alt_client = None
            if alt_client is not None:
                alt_llm = alt_client

                async def _alt_chat(messages, stream: bool = False):
                    resp, _meta = await alt_llm.chat(messages, stream=stream)
                    return resp, _meta

                secondary_chat = _alt_chat
                # Override model name with the alternate's actual model so the
                # review manager calls the correct endpoint.
                if alt_client.model:
                    alternate = alt_client.model
                import logging as _logging

                _logging.getLogger(__name__).info(
                    "P14-2 dual-review: primary=%s (%s) | secondary=%s (%s)",
                    primary_model,
                    getattr(self.llm, "provider", "?"),
                    alternate,
                    getattr(alt_client, "provider", "?"),
                )

        return DualReviewManager(
            primary_chat=primary_chat,
            secondary_chat=secondary_chat,
            primary_model=primary_model,
            secondary_model=alternate,
        )

    async def _dual_review_hook(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see DualReviewHook."""
        return await self.dual_review_hook(payload)

    async def _review_state_transition_hook(self, payload):
        """P12-3: Transition to REVIEW when a high-risk tool triggers dual review.

        Registered alongside ``_dual_review_hook`` on BEFORE_TOOL_EXECUTION.
        Only triggers the state transition when dual review would actually
        fire (high-risk tool + dual_review enabled). Never blocks the payload.
        """
        if not isinstance(payload, dict):
            return payload
        if self.dual_review is None:
            return payload
        tool_name = payload.get("tool", "")
        if self.dual_review.is_high_risk(tool_name):
            self._task_state_transition(TaskState.REVIEW)
        return payload

    async def _ab_apply_variants_hook(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see ABTestApplyHook."""
        if self.ab_apply_hook is None:
            return payload
        return await self.ab_apply_hook(payload)

    async def _ab_record_observation_hook(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see ABTestRecordObservationHook."""
        if self.ab_record_hook is None:
            return payload
        return await self.ab_record_hook(payload)

    async def _inject_progress_hook(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see ProgressInjectHook."""
        if self.progress_inject_hook is None:
            return payload
        return await self.progress_inject_hook(payload)

    async def _update_progress_hook(self, payload):  # back-compat shim
        """PR-19: Back-compat shim — see ProgressUpdateHook."""
        if self.progress_update_hook is None:
            return payload
        return await self.progress_update_hook(payload)

    @staticmethod
    def _extract_step_num(step_str: str) -> int:
        """PR-19: Back-compat shim for the static method.

        The implementation moved to `extract_step_num()` in
        `agent/hooks/progress.py`. Kept here so existing test
        calls (e.g. `AgentEngine._extract_step_num("3/8")`) still work.
        """
        from ..hooks.progress import extract_step_num

        return extract_step_num(step_str)

    async def shutdown(self):
        """Shut down engine resources — MCP servers, sub-agents, running tasks."""
        # Cancel all running sub-agents
        from .subagent_registry import get_registry

        try:
            sub_reg = get_registry()
            for agent_id in list(sub_reg._records.keys()):
                record = sub_reg._records.get(agent_id)
                if record and record.get("status") == "running":
                    sub_reg.fail(agent_id, "Parent engine shut down")
        except Exception:
            pass

        # Stop MCP servers
        if self._mcp_manager:
            await self._mcp_manager.stop_all()
            self._mcp_manager = None

    async def run_stream(self, task: str, plan_context: str = ""):
        """Run agent with streaming output, yielding SSE events.

        Yields dicts with keys: type, content, step, tool_name, tool_args, tool_result

        If plan_context is provided, it is injected into the system prompt
        so the agent executes within the context of an approved plan.
        """

        _log_event(self.trace_id, "run_stream_start", task=task[:50])

        # PR-14: Fire ON_SESSION_START at the very beginning of a session.
        # This loads the user profile (if any) into the engine so it can
        # be injected into the system prompt on the first LLM call.
        try:
            await self.hooks.execute(
                ON_SESSION_START,
                {
                    "session_id": self.trace_id,
                    "task": task,
                },
            )
        except Exception:
            # Never let a session-start hook failure block the run
            pass

        # PR-12: A/B test session tracking. Capture start time + last
        # task so the ON_SESSION_END hook can record observations.
        if self.ab_test is not None:
            import time as _time

            self._ab_task_start_ts = _time.time()
            self._ab_last_task = task
            self._ab_experiments_in_flight = []

        # Initialize MCP servers on first run if enabled
        await self._ensure_mcp_initialized()

        # Auto-detect project directory for complex tasks
        task_lower = task.lower()
        if any(kw in task_lower for kw in _COMPLEX_KEYWORDS):
            self.current_project_dir = generate_project_name(task)

        # Search for relevant skills
        skill_prompt = self.skills.activate_skills_semantic(task)

        # PR-14 → PR-15 → L2+L3+L4+M1: Auto-extract user facts.
        # L3: use FactConfirmExtractor (two-stage: extract → LLM-confirm → apply).
        # M1: pass prior conversation as `history` so the LLM has context
        #     for cases like "5 turns ago I said X, this turn I asked 'who am I?'".
        # CRITICAL: only extract from the [Current task] section if the
        # task is wrapped in the CLI's "[Previous conversation]...[Current
        # task]..." format. The "[Previous conversation]" portion is
        # routed to the LLM as context (NOT as extraction target).
        if self.config.auto_remember_user_facts and self.user_profile is not None:
            try:
                from .fact_extractor import FactConfirmExtractor

                extractor = FactConfirmExtractor(
                    llm_client=self.llm,
                    use_llm=getattr(self.config, "fact_extraction_use_llm", True),
                    fallback_to_legacy=True,  # always — safety net
                )
                # Split the CLI-prefixed task into (history, current_target).
                history_text = ""
                extract_target = task
                marker = "[Current task]"
                if marker in task:
                    parts = task.split(marker, 1)
                    history_text = parts[0].replace("[Previous conversation]", "").strip()
                    extract_target = parts[1].strip()
                # Async LLM-first with history injection. The L3 gate
                # silently drops low-confidence facts before they reach
                # the profile; L2 (UserProfile.remember_fact) is the
                # last-line schema check.
                facts = await extractor.extract_and_apply_async(
                    extract_target,
                    self.user_profile,
                    history=history_text,
                )
                if facts:
                    _log_event(
                        self.trace_id,
                        "auto_remember",
                        facts=[f"{k}={v}" for k, v in facts],
                    )
            except Exception:
                # Never let auto-extract failure block the run
                pass

        # P12-3: Initialize task state machine for this task. Resets the record
        # so a fresh start doesn't carry over completed_steps from a prior task.
        try:
            self.task_state_machine.start_task(task=task, session_id=self.trace_id)
        except Exception:
            pass
        # Transition INIT → PLAN at session start (planning phase begins).
        self._task_state_transition(TaskState.PLAN, current_step={"description": "planning"})

        # Build system prompt
        plan_ctx = plan_context if plan_context else ""
        failure_ctx = self.evolution.get_failure_context(task) if self.config.auto_evolve else ""
        # P12-3: inject task-state reminder so the LLM knows the current phase
        # and the count of completed steps. Helps long-running tasks stay on
        # track (50+ steps easily drift without this anchor).
        task_reminder = ""
        try:
            task_reminder = self.task_state_machine.format_reminder()
        except Exception:
            pass
        system = self._get_system_prompt(
            skill_prompt=skill_prompt,
            plan_context=plan_ctx,
            failure_context=failure_ctx,
        )
        if task_reminder:
            system = system + "\n\n" + task_reminder

        self.memory.add("system", system)
        self.memory.add("user", task)
        if self.current_project_dir:
            self.memory.add("user", f"[Current project: {self.current_project_dir}/]")

        # PR-01: Hook + EventBus fires for task perception
        await self.hooks.execute(BEFORE_PERCEIVE, {"task": task})
        await self.event_bus.emit(BEFORE_PERCEIVE, {"task": task})

        try:
            async for event in self._run_stream_loop(task, system):
                yield event
            # P12-3: loop exited without exception → mark task DONE.
            self._task_state_transition(TaskState.DONE)
        except Exception as exc:
            # P12-3: exception → mark task FAILED (recoverable via INIT/PLAN).
            self._task_state_transition(TaskState.FAILED)
            # PR-01: ON_ERROR hook for global exception handling
            try:
                await self.hooks.execute(ON_ERROR, {"exception": exc, "context": {"task": task}})
            except Exception:
                pass
            raise
        finally:
            # PR-01: ON_SESSION_END hook for cleanup
            try:
                await self.hooks.execute(
                    ON_SESSION_END,
                    {
                        "final_state": {"trace_id": self.trace_id},
                        "result": None,
                    },
                )
                await self.event_bus.emit(ON_SESSION_END, {"trace_id": self.trace_id})
            except Exception:
                pass

    async def _build_step_messages(self) -> list:
        """PR-22: Build the LLM message list for a single step.

        Materializes working memory into Message objects and injects the
        per-turn `<system-reminder>` (cwd, git status, plan progress) into
        the last user message — keeping the system prompt cache-stable.
        """
        mem_messages = self.memory.get_messages()
        messages = []
        for m in mem_messages:
            msg = Message(
                role=m.role,
                content=m.content,
                tool_call_id=m.tool_call_id,
            )
            if m.tool_calls:
                msg.tool_calls = m.tool_calls
            messages.append(msg)
        env = self._get_env_context()
        reminder = PromptAssembler.build_system_reminder(**env)
        if reminder:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].role == "user":
                    messages[i] = Message(
                        role="user",
                        content=messages[i].content + "\n\n" + reminder,
                        tool_call_id=messages[i].tool_call_id,
                    )
                    break
        return messages

    async def _consume_stream(self, response, state: dict):
        """PR-22: Async generator — consume streaming LLM response.

        Strips leaked `<minimax:tool_call>` / `<tool_call>` protocol tags from
        content (some models emit them in the content stream by accident),
        fires the ON_TOKEN hook + event_bus per delta, and aggregates tool
        call fragments by ``index``.

        Yields ``{"type": "content", "content": raw}`` events for the CLI
        to render as tokens arrive. The final ``(full_content, tool_calls,
        usage)`` tuple is written into ``state`` (a caller-provided dict)
        since async generators cannot both yield AND return a value to the
        caller in Python.
        """
        full_content = ""
        accumulated_tool_calls: dict = {}
        _in_think = False
        _usage = None

        if not hasattr(response, "__iter__"):
            # Non-streaming response (rare — defensive).
            state["full_content"] = full_content
            state["tool_calls"] = accumulated_tool_calls
            state["usage"] = _usage
            # Per-turn: track whether the LLM actually reported usage
            self._last_usage_estimated = _usage is None
            return

        import re as _re_filter

        _tag_patterns = [
            _re_filter.compile(r"<minimax:tool_call>.*?</minimax:tool_call>", _re_filter.DOTALL),
            _re_filter.compile(r"<tool_call>.*?</tool_call>", _re_filter.DOTALL),
        ]
        for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                _usage = {
                    "input_tokens": getattr(chunk.usage, "input_tokens", 0)
                    or getattr(chunk.usage, "prompt_tokens", 0),
                    "output_tokens": getattr(chunk.usage, "output_tokens", 0)
                    or getattr(chunk.usage, "completion_tokens", 0),
                }
            if not (hasattr(chunk, "choices") and chunk.choices):
                continue
            delta = chunk.choices[0].delta
            # ---- Content delta ----
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
                    for pat in _tag_patterns:
                        raw = pat.sub("", raw)
                    if raw:
                        full_content += raw
                        await self.hooks.execute(ON_TOKEN, {"chunk": raw})
                        await self.event_bus.emit(ON_TOKEN, {"chunk": raw})
                        yield {"type": "content", "content": raw}
            # ---- Tool call delta aggregation ----
            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = getattr(tc, "index", 0)
                    if idx not in accumulated_tool_calls:
                        accumulated_tool_calls[idx] = {
                            "id": None,
                            "name": "",
                            "arguments": "",
                        }
                    if getattr(tc, "id", None):
                        accumulated_tool_calls[idx]["id"] = tc.id
                    if hasattr(tc, "function"):
                        if getattr(tc.function, "name", None):
                            accumulated_tool_calls[idx]["name"] = tc.function.name
                        if getattr(tc.function, "arguments", None):
                            accumulated_tool_calls[idx]["arguments"] += tc.function.arguments

        state["full_content"] = full_content
        state["tool_calls"] = accumulated_tool_calls
        state["usage"] = _usage
        # Per-turn: track whether the LLM actually reported usage, so
        # the UI can show a "(估计)" tag when it didn't.
        self._last_usage_estimated = _usage is None

    async def _dispatch_tool_calls(self, parsed: list, step: int):
        """PR-22: Async generator — partition, execute, yield tool events.

        Concurrent-safe tools run in parallel via ``asyncio.gather``;
        write tools serialize. Yields ``tool_call`` events first (in the
        LLM's original order), then ``tool_result`` events.
        """
        for p in parsed:
            yield {
                "type": "tool_call",
                "tool_name": p["func_name"],
                "tool_args": p["args"],
                "tool_call_id": p["tc_id"],
                "step": step,
            }
        concurrent_tcs, serial_tcs = self._partition_tool_calls(parsed)
        results_by_id: dict = {}

        if concurrent_tcs:

            async def _run_concurrent(tc):
                return (
                    tc["tc_id"],
                    await self._execute_tool(
                        tc["func_name"],
                        tc["args"],
                        tc["tc_id"],
                        tc["func_args_raw"],
                    ),
                )

            gather_results = await asyncio.gather(
                *[_run_concurrent(tc) for tc in concurrent_tcs],
                return_exceptions=True,
            )
            for item in gather_results:
                if isinstance(item, Exception):
                    # Sibling failure — don't crash the loop; the missing
                    # tc_id will be filled with a "sibling abort" result below.
                    pass
                elif isinstance(item, tuple):
                    tc_id, result = item
                    results_by_id[tc_id] = result

        for tc in serial_tcs:
            result = await self._execute_tool(
                tc["func_name"],
                tc["args"],
                tc["tc_id"],
                tc["func_args_raw"],
            )
            results_by_id[tc["tc_id"]] = result

        for p in parsed:
            result = results_by_id.get(p["tc_id"])
            if result is None:
                result = ToolResult(
                    success=False,
                    content="",
                    error="Tool execution failed (sibling abort)",
                )
            yield {
                "type": "tool_result",
                "success": result.success,
                "content": result.content,
                "error": result.error,
                "metadata": result.metadata,
                "tool_name": p["func_name"],
                "tool_call_id": p["tc_id"],
            }

    async def _run_stream_loop(self, task: str, system: str):
        """Inner ReAct loop — yields events, hook points fired here.

        PR-22: decomposed into focused helpers — message building, stream
        consumption, tool dispatch — so the loop itself reads top-to-bottom
        as the agent's control flow rather than as 200+ lines of nested
        streaming/tool/result logic.
        """
        for step in range(1, self.config.max_steps + 1):
            yield {"type": "step_start", "step": step, "max_steps": self.config.max_steps}

            if self.llm is None:
                yield {"type": "error", "error": "No LLM configured"}
                return

            messages = await self._build_step_messages()
            yield {"type": "thinking", "content": ""}

            # PR-01: BEFORE_LLM_CALL hook (codmap, spec ACs, progress, AB tests)
            llm_payload = {"messages": messages, "system": system, "step": step}
            llm_payload = await self.hooks.execute(BEFORE_LLM_CALL, llm_payload)
            messages = llm_payload["messages"]
            await self.event_bus.emit(BEFORE_LLM_CALL, {"step": step})

            response, _ = await self.llm.chat(
                messages=messages,
                tools=registry.schemas,
                stream=True,
            )

            # Consume streaming chunks. The helper yields content events
            # for the CLI; final state (full_content, tool_calls, usage) is
            # written into a dict so the caller can read it after iteration.
            _stream_state: dict = {}
            async for event in self._consume_stream(response, _stream_state):
                yield event
            full_content = _stream_state.get("full_content", "")
            accumulated_tool_calls = _stream_state.get("tool_calls", {})
            _usage = _stream_state.get("usage")

            # PR-01: AFTER_LLM_CALL hook (after streaming completes)
            await self.hooks.execute(
                AFTER_LLM_CALL,
                {
                    "response": response,
                    "usage": _usage,
                    "content": full_content,
                },
            )
            await self.event_bus.emit(
                AFTER_LLM_CALL,
                {
                    "usage": _usage,
                    "content_length": len(full_content),
                },
            )
            yield {"type": "content_end", "content": full_content}

            # Track token usage for auto-compact
            if _usage:
                self._total_input_tokens += _usage.get("input_tokens", 0)
                self._total_output_tokens += _usage.get("output_tokens", 0)
                total = self._total_input_tokens + self._total_output_tokens
                if total > self._context_window * self._compact_threshold:
                    self.memory.compact(
                        f"Previous context ({total} tokens). Continuing task: {task[:100]}"
                    )

            # Tool calls?
            tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None
            if tool_calls:
                # P12-3: first tool execution → transition PLAN → EXEC. This
                # anchors the state machine to actual execution activity.
                self._task_state_transition(TaskState.EXEC)
                # P12-3: detect run_tests up front so a single batch can
                # transition PLAN → EXEC → TEST cleanly.
                if any(tc.get("name") == "run_tests" for tc in tool_calls):
                    self._task_state_transition(TaskState.TEST)
                parsed = []
                for tc in tool_calls:
                    tc_id = tc.get("id")
                    func_name = tc.get("name")
                    func_args_raw = tc.get("arguments", "{}")
                    if not tc_id or not func_name:
                        continue
                    try:
                        func_args = json.loads(func_args_raw) if func_args_raw else {}
                    except json.JSONDecodeError:
                        func_args = {}
                    parsed.append(
                        {
                            "tc_id": tc_id,
                            "func_name": func_name,
                            "func_args_raw": func_args_raw,
                            "args": func_args if isinstance(func_args, dict) else {},
                        }
                    )
                async for event in self._dispatch_tool_calls(parsed, step):
                    yield event
            elif full_content:
                # Plain text response (no tool calls)
                self.memory.add("assistant", full_content)
                evolution_result = self.evolution.analyze_run(task, self.memory)
                if evolution_result.get("actions"):
                    yield {"type": "evolution", "actions": evolution_result["actions"]}
                final_event = {"type": "final", "content": full_content}
                if _usage:
                    final_event["usage"] = _usage
                    final_event["estimated"] = False
                    if self._context_window:
                        final_event["context"] = {
                            "used": _usage.get("input_tokens", 0),
                            "window": self._context_window,
                        }
                else:
                    # LLM didn't report usage — estimate both sides so
                    # the display doesn't lie. Mark as estimated so the
                    # UI can show a "(估计)" label.
                    msg_text = " ".join(
                        (
                            m.get("content", "")
                            if isinstance(m, dict)
                            else (getattr(m, "content", "") or "")
                        )
                        for m in (messages or [])
                    )
                    final_event["usage"] = {
                        "input_tokens": self._estimate_text_tokens(msg_text),
                        "output_tokens": self._estimate_text_tokens(full_content),
                    }
                    final_event["estimated"] = True
                self._last_usage_estimated = final_event.get("estimated", False)
                yield final_event
                return

        evolution_result = self.evolution.analyze_run(task, self.memory)
        if evolution_result.get("actions"):
            yield {"type": "evolution", "actions": evolution_result["actions"]}
        yield {
            "type": "complete",
            "content": (
                f"Task hit step limit ({self.config.max_steps}) — "
                f"you can continue by asking me to pick up where I left off"
            ),
        }

        for step in range(1, self.config.max_steps + 1):
            yield {"type": "step_start", "step": step, "max_steps": self.config.max_steps}

            if self.llm is None:
                yield {"type": "error", "error": "No LLM configured"}
                return

            messages = await self._build_step_messages()
            yield {"type": "thinking", "content": ""}

            # PR-01: BEFORE_LLM_CALL hook (codmap, spec ACs, progress, AB tests)
            llm_payload = {"messages": messages, "system": system, "step": step}
            llm_payload = await self.hooks.execute(BEFORE_LLM_CALL, llm_payload)
            messages = llm_payload["messages"]
            await self.event_bus.emit(BEFORE_LLM_CALL, {"step": step})

            response, _ = await self.llm.chat(
                messages=messages,
                tools=registry.schemas,
                stream=True,
            )

            # Consume streaming chunks → (content, tool_calls, usage).
            # The content tokens yielded by the consumer stream back to the
            # outer iterator so the CLI sees them as they arrive.
            full_content, accumulated_tool_calls, _usage = (
                await _consume_stream_to_completion(  # noqa: F821
                    self,
                    response,
                )
            )
            # Replay any content events the consumer emitted (we returned
            # them through the coroutine via the async generator pattern).
            # Note: the consumer above is itself a generator — see the
            # wrapper below for how we bridge async generators + return value.

            # PR-01: AFTER_LLM_CALL hook (after streaming completes)
            await self.hooks.execute(
                AFTER_LLM_CALL,
                {
                    "response": response,
                    "usage": _usage,
                    "content": full_content,
                },
            )
            await self.event_bus.emit(
                AFTER_LLM_CALL,
                {
                    "usage": _usage,
                    "content_length": len(full_content),
                },
            )
            yield {"type": "content_end", "content": full_content}

            # Track token usage for auto-compact
            if _usage:
                self._total_input_tokens += _usage.get("input_tokens", 0)
                self._total_output_tokens += _usage.get("output_tokens", 0)
                total = self._total_input_tokens + self._total_output_tokens
                if total > self._context_window * self._compact_threshold:
                    self.memory.compact(
                        f"Previous context ({total} tokens). Continuing task: {task[:100]}"
                    )

            # Tool calls?
            tool_calls = list(accumulated_tool_calls.values()) if accumulated_tool_calls else None
            if tool_calls:
                # P12-3: first tool execution → transition PLAN → EXEC. This
                # anchors the state machine to actual execution activity.
                self._task_state_transition(TaskState.EXEC)
                # P12-3: detect run_tests up front so a single batch can
                # transition PLAN → EXEC → TEST cleanly.
                if any(tc.get("name") == "run_tests" for tc in tool_calls):
                    self._task_state_transition(TaskState.TEST)
                parsed = []
                for tc in tool_calls:
                    tc_id = tc.get("id")
                    func_name = tc.get("name")
                    func_args_raw = tc.get("arguments", "{}")
                    if not tc_id or not func_name:
                        continue
                    try:
                        func_args = json.loads(func_args_raw) if func_args_raw else {}
                    except json.JSONDecodeError:
                        func_args = {}
                    parsed.append(
                        {
                            "tc_id": tc_id,
                            "func_name": func_name,
                            "func_args_raw": func_args_raw,
                            "args": func_args if isinstance(func_args, dict) else {},
                        }
                    )
                async for event in self._dispatch_tool_calls(parsed, step):
                    yield event
            elif full_content:
                # Plain text response (no tool calls)
                self.memory.add("assistant", full_content)
                evolution_result = self.evolution.analyze_run(task, self.memory)
                if evolution_result.get("actions"):
                    yield {"type": "evolution", "actions": evolution_result["actions"]}
                final_event = {"type": "final", "content": full_content}
                if _usage:
                    final_event["usage"] = _usage
                    final_event["estimated"] = False
                    if self._context_window:
                        final_event["context"] = {
                            "used": _usage.get("input_tokens", 0),
                            "window": self._context_window,
                        }
                else:
                    # LLM didn't report usage — estimate both sides so
                    # the display doesn't lie. Mark as estimated so the
                    # UI can show a "(估计)" label.
                    msg_text = " ".join(
                        (
                            m.get("content", "")
                            if isinstance(m, dict)
                            else (getattr(m, "content", "") or "")
                        )
                        for m in (messages or [])
                    )
                    final_event["usage"] = {
                        "input_tokens": self._estimate_text_tokens(msg_text),
                        "output_tokens": self._estimate_text_tokens(full_content),
                    }
                    final_event["estimated"] = True
                self._last_usage_estimated = final_event.get("estimated", False)
                yield final_event
                return

        evolution_result = self.evolution.analyze_run(task, self.memory)
        if evolution_result.get("actions"):
            yield {"type": "evolution", "actions": evolution_result["actions"]}
        yield {
            "type": "complete",
            "content": (
                f"Task hit step limit ({self.config.max_steps}) — "
                f"you can continue by asking me to pick up where I left off"
            ),
        }

    async def run(self, task: str, plan_context: str = "") -> str:
        """Run the agent on a task with memory, skills, and permissions."""
        await self._ensure_mcp_initialized()
        _log_event(self.trace_id, "run_start", task=task[:50])
        if self.config.verbose:
            print(
                f"\n{Colors.BOLD}🎯 Task:{Colors.RESET} {task[:100]}{'...' if len(task) > 100 else ''}"
            )
            print()

        # Auto-detect project directory for complex tasks
        task_lower = task.lower()
        if any(kw in task_lower for kw in _COMPLEX_KEYWORDS):
            self.current_project_dir = generate_project_name(task)

        # Search for relevant skills
        skill_prompt = self.skills.activate_skills_semantic(task)

        # Build system prompt
        plan_ctx = plan_context if plan_context else ""
        failure_ctx = self.evolution.get_failure_context(task) if self.config.auto_evolve else ""
        system = self._get_system_prompt(
            skill_prompt=skill_prompt,
            plan_context=plan_ctx,
            failure_context=failure_ctx,
        )

        self.memory.add("system", system)
        self.memory.add("user", task)
        if self.current_project_dir:
            self.memory.add("user", f"[Current project: {self.current_project_dir}/]")

        for step in range(1, self.config.max_steps + 1):
            if self.config.verbose:
                print_step(step, self.config.max_steps, "processing")

            # Build messages from memory
            mem_messages = self.memory.get_messages()
            messages = []
            for m in mem_messages:
                msg = Message(role=m.role, content=m.content, tool_call_id=m.tool_call_id)
                if m.tool_calls:
                    msg.tool_calls = m.tool_calls
                messages.append(msg)

            # Inject system-reminder into last user message
            env = self._get_env_context()
            reminder = PromptAssembler.build_system_reminder(**env)
            if reminder:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].role == "user":
                        messages[i] = Message(
                            role="user",
                            content=messages[i].content + "\n\n" + reminder,
                            tool_call_id=messages[i].tool_call_id,
                        )
                        break

            # Get LLM response with tool schemas
            if self.llm is None:
                if self.config.verbose:
                    print("Error: No LLM configured")
                return "No LLM configured"

            if self.config.verbose:
                print_thinking()

            response = await self.llm.chat(messages=messages, tools=registry.schemas)

            # Parse response
            if isinstance(response, str):
                self.memory.add("assistant", response)
                if self.config.verbose:
                    print_done(response)
                self.evolution.analyze_run(task, self.memory)
                return response

            # Handle tool call response (OpenAI format)
            if hasattr(response, "tool_calls") and response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        args = (
                            json.loads(tool_call.function.arguments)
                            if tool_call.function.arguments
                            else {}
                        )
                    except json.JSONDecodeError:
                        args = {}

                    if self.config.verbose:
                        print_tool_call(tool_name, args)

                    func_args_raw = (
                        tool_call.function.arguments if hasattr(tool_call, "function") else "{}"
                    )
                    result = await self._execute_tool(tool_name, args, tool_call.id, func_args_raw)

                    if result.success:
                        self._consecutive_failures.pop(tool_name, None)
                    else:
                        self._consecutive_failures[tool_name] = (
                            self._consecutive_failures.get(tool_name, 0) + 1
                        )

                    if self.config.verbose:
                        print_tool_result(result.success, result.content, result.error)
            else:
                # Plain text response
                content = response.content if hasattr(response, "content") else str(response)
                self.memory.add("assistant", content)
                if self.config.verbose:
                    print_done(content)
                self.evolution.analyze_run(task, self.memory)
                return content

        self.evolution.analyze_run(task, self.memory)
        return f"Task hit step limit ({self.config.max_steps}) — you can continue by asking me to pick up where I left off"

    async def run_plan(self, task: str) -> ExecutionPlan:
        """PR-21: thin shim — delegates to PlanWorkflow.plan()."""
        return await PlanWorkflow(self).plan(task)

    async def run_with_evaluator(
        self,
        task: str,
        plan_context: str = "",
        workspace: "Optional[Path]" = None,
    ):
        """P13-5: Run the agent and write an evaluation report (SCORE.md).

        Wraps ``run_stream()`` — collects events, lets the loop finish, then
        instantiates an EvaluatorAgent and writes ``SCORE.md`` + ``.score.json``
        to the workspace. Returns the EvaluationReport for programmatic use.

        Args:
            task: The task description.
            plan_context: Optional plan context (passed to run_stream).
            workspace: Directory to write SCORE.md into. Defaults to cwd.

        Notes:
            - Failures from the evaluator are logged but never block the
              agent run — evaluation is an observability tool, not a gate.
            - The evaluator picks a cross-family judge automatically when
              self.llm is set (GPT primary → Claude judge and vice versa).
        """
        import logging as _logging
        from pathlib import Path

        log = _logging.getLogger(__name__)
        # 1. Run the agent to completion (collect events; we don't re-emit)
        final_content = ""
        audit_records: list = []
        async for event in self.run_stream(task=task, plan_context=plan_context):
            # Capture the final content + tool audit for the evaluator
            if event.get("type") == "final":
                final_content = event.get("content", "")
            if event.get("type") == "tool_call":
                audit_records.append(
                    {
                        "action": "tool_call",
                        "tool": event.get("tool_name"),
                        "args": event.get("tool_args"),
                    }
                )
            if event.get("type") == "tool_result":
                audit_records.append(
                    {
                        "action": "tool_result",
                        "tool": event.get("tool_name"),
                        "success": event.get("success"),
                        "error": event.get("error"),
                    }
                )

        # 2. Build evaluator with cross-family judge
        from agent.agents.evaluator import EvaluatorAgent

        evaluator = EvaluatorAgent(self)
        # 3. Run evaluation
        try:
            report = await evaluator.evaluate(
                task=task, agent_id="main", audit_records=audit_records
            )
        except Exception as e:
            log.warning("P13-5: evaluator.evaluate() failed: %s", e)
            return None

        # 4. Write SCORE.md + .score.json to workspace
        target_workspace = Path(workspace) if workspace else Path.cwd()
        try:
            md_path, json_path = EvaluatorAgent.write_report(report, workspace=target_workspace)
            log.info("P13-5: evaluation written to %s", md_path)
        except Exception as e:
            log.warning("P13-5: write_report failed: %s", e)

        return report
        return await self.plan_workflow.plan(task)

    async def run_execute(self, plan: ExecutionPlan) -> str:
        """PR-21: thin shim — delegates to PlanWorkflow.execute()."""
        return await self.plan_workflow.execute(plan)

    async def run_with_orchestrator(self, task: str) -> str:
        """PR-07: Run a complex task through the Orchestrator PM Agent.

        Decomposes the task into a DAG of subtasks, dispatches each to a
        role-specialized sub-agent (via EventBus), then merges the results.

        For most calls, prefer the regular `run()` / `run_stream()` paths.
        Use this when the task explicitly requires multi-role coordination
        (e.g., "implement + test + review + deploy").
        """
        from ..agents import OrchestratorAgent
        from ..agents.orchestrator import TaskRequest, TaskResponse

        await self._ensure_mcp_initialized()
        _log_event(self.trace_id, "orchestrator_start", task=task[:80])

        async def llm_call(prompt: str) -> str:
            """Lightweight LLM invocation for decompose/merge steps."""
            if not self.llm:
                # No LLM available (e.g. test mode) — refuse politely.
                raise RuntimeError("Orchestrator needs an LLM but engine.llm is None")
            from ..llm.client import Message

            resp, _meta = await self.llm.chat(
                [Message(role="user", content=prompt)],
                stream=False,
            )
            return resp

        async def dispatch_fn(req: "TaskRequest", completed):
            """Dispatch a subtask to a sub-agent and return a TaskResponse.

            For now, the sub-agent is the same LLM with a role-specific
            system prompt addon. A future PR can replace this with a
            dedicated sub-agent executor.
            """
            from ..agents.roles import BUILTIN_ROLES
            from ..llm.client import Message

            role = BUILTIN_ROLES.get(req.role) or BUILTIN_ROLES["code"]
            sys_prompt = role.system_prompt_addon
            context_str = "\n".join(
                f"[{tid}] {r.outputs.get('summary', r.error or '')}" for tid, r in completed.items()
            )
            user_prompt = req.description
            if context_str:
                user_prompt += f"\n\nUpstream context:\n{context_str}"
            try:
                resp, _ = await self.llm.chat(
                    [
                        Message(role="system", content=sys_prompt),
                        Message(role="user", content=user_prompt),
                    ],
                    stream=False,
                )
            except Exception as e:
                return TaskResponse(
                    task_id=req.task_id,
                    status="failed",
                    role=req.role,
                    description=req.description,
                    error=str(e),
                )
            return TaskResponse(
                task_id=req.task_id,
                status="done",
                role=req.role,
                description=req.description,
                outputs={"summary": resp, "raw": resp},
            )

        orchestrator = OrchestratorAgent(
            llm_call=llm_call,
            dispatch_fn=dispatch_fn,
        )

        try:
            result = await orchestrator.run(task)
        except Exception as e:
            _log_event(self.trace_id, "orchestrator_error", error=str(e))
            raise

        _log_event(self.trace_id, "orchestrator_done", task=task[:80])
        return result

    async def run_with_plan(self, task: str, auto_confirm: bool = False):
        """Plan-then-execute workflow with streaming events for CLI interaction.

        Yields events:
            {"type": "plan", "plan": plan_dict}
            {"type": "plan_confirm", "plan": plan_dict}  # waits for user
            {"type": "execute_start"}
            ... normal streaming events ...
            {"type": "done"}
        """
        await self._ensure_mcp_initialized()
        # Phase 1: Plan
        plan = await self.run_plan(task)
        yield {"type": "plan", "plan": plan.to_dict(), "markdown": plan.to_markdown()}

        # Phase 2: Confirm (caller handles via yield, engine just provides the plan)
        if not auto_confirm:
            yield {"type": "plan_confirm", "plan": plan.to_dict()}
            return  # Caller must call run_execute() separately
        else:
            yield {"type": "execute_start"}
            result = await self.run_execute(plan)
            yield {"type": "done", "result": result}


async def run_agent(
    task: str, model: str = None, provider: str = None, mode: str = None, verbose: bool = True
) -> str:
    """Run a single task with the agent. Defaults from config.json then env."""
    # Load config file if exists
    config_data = _load_config_file()

    # Env vars override config file; explicit args override env vars
    model = model or _cfg.get("model")
    provider = provider or _cfg.get("provider")
    mode = mode or _cfg.get("mode")

    config = AgentConfig(
        model=model,
        provider=provider,
        mode=mode,
        verbose=verbose,
        max_steps=config_data.get("max_steps", 20),
        max_tool_retries=config_data.get("max_tool_retries", 1),
    )
    agent = AgentEngine(config)
    try:
        return await agent.run(task)
    finally:
        await agent.shutdown()


if __name__ == "__main__":
    import sys

    async def main():
        print("Coding Agent - Phase 8", flush=True)
        print("=" * 40, flush=True)

        task = (
            sys.argv[1]
            if len(sys.argv) > 1
            else "用 Python 写一个斐波那契函数，保存到 fibonacci.py，然后运行它"
        )
        print(f"Task: {task}", flush=True)
        print("-" * 40, flush=True)

        try:
            result = await run_agent(task)
            print(f"\nResult:\n{result}", flush=True)
        except Exception as e:
            print(f"\nError: {e}", flush=True)

    asyncio.run(main())
