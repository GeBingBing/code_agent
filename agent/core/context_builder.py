"""ContextBuilder — owns context sources (project, spec, codmap) and prompt assembly.

Extracted from AgentEngine (PR-20). Single Responsibility: turn on-disk state
(CODING_AGENT.md, SPECS.md, repo map, active spec ACs) plus engine state
(memory, user profile) into the LLM-bound prompt payload.

Before PR-20 these four concerns were inlined on AgentEngine:
  - _get_system_prompt        (assemble system prompt)
  - _load_project_context     (read CODING_AGENT.md)
  - _inject_codmap            (BEFORE_LLM_CALL: append repo map to last user msg)
  - _inject_spec_acs          (BEFORE_LLM_CALL: append pending ACs to last user msg)

Pulling them out lets AgentEngine shed ~107 lines of context-rendering code
and gives context-loading a single, testable seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..prompts.assembler import PromptAssembler


class ContextBuilder:
    """Build the LLM-facing prompt context for an engine run.

    Constructor takes runtime-mutable dependencies (config, memory, profile)
    plus an immutable workspace path. Two `BeforeLlmCallHook`-shaped methods
    (inject_codmap, inject_spec_acs) are designed to be registered directly
    with the engine's HookRegistry — they accept a payload dict and return
    it (possibly mutated in-place) to keep the registry's "fire and replace"
    contract.
    """

    def __init__(
        self,
        config,
        memory,
        user_profile=None,
        workspace: Optional[Path] = None,
    ):
        self._config = config
        self._memory = memory
        self._user_profile = user_profile
        self._workspace = workspace

        # PR-05: codmap generator (Aider-style repo map). Lazily None when
        # disabled or init fails — tests with non-real workspaces rely on this.
        self._codmap = None
        if getattr(config, "codmap_enabled", True):
            try:
                from index.codmap import CodmapGenerator
                self._codmap = CodmapGenerator(workspace=workspace)
            except Exception:
                # Don't crash the engine if codmap init fails (bad path, etc.)
                self._codmap = None

        # PR-06: AC-aware spec document. spec_context is the legacy
        # "to_prompt()" string form; spec_document is the structured form
        # with phase/AC objects. We load both — context strings for the
        # system prompt, the structured form for AC injection.
        from .spec_loader import load_spec, load_spec_document
        self.spec_context = load_spec(workspace)
        self.spec_document = None
        try:
            self.spec_document = load_spec_document(workspace)
        except Exception:
            self.spec_document = None

        # Project-level instructions (CODING_AGENT.md).
        self.project_context = self._load_project_context()

    # ── Prompt assembly ─────────────────────────────────────────────

    def _load_project_context(self) -> str:
        """Load project instructions from WORKSPACE/CODING_AGENT.md if present."""
        if self._workspace is None:
            return ""
        ctx_file = Path(self._workspace) / "CODING_AGENT.md"
        if ctx_file.exists():
            try:
                return ctx_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
        return ""

    def get_system_prompt(
        self,
        task: str = "",
        skill_prompt: str = "",
        plan_context: str = "",
        failure_context: str = "",
    ) -> str:
        """Build the full system prompt, respecting any custom override.

        Layered from most-stable (identity, instructions) to most-dynamic
        (plan, failure context). Custom override short-circuits everything.
        """
        if self._config.custom_system_prompt:
            return self._config.custom_system_prompt
        # PR-14: user_profile rendered as <user_profile> XML before <memory>
        # so the agent sees identity before generic long-term facts.
        user_profile_prompt = (
            self._user_profile.to_prompt() if self._user_profile else ""
        )
        return PromptAssembler.build_system_prompt(
            long_term_memory=self._memory.get_long_term_context(),
            skill_prompt=skill_prompt,
            project_context=self.project_context,
            plan_context=plan_context,
            spec_context=self.spec_context.to_prompt() if self.spec_context else "",
            failure_context=failure_context,
            user_profile=user_profile_prompt,
        )

    # ── BEFORE_LLM_CALL hooks ───────────────────────────────────────

    async def inject_codmap(self, payload: Any) -> Any:
        """PR-05: Append a compact repo map to the last user message.

        Why **last user message** rather than system prompt?
          - The system prompt is often cached (prompt cache). Mutating it
            busts the cache. System-reminder injected into the user message
            keeps the cache prefix stable.
          - We append to the LAST user message so the LLM sees the map on
            the current turn without context-washing prior turns.
        """
        if self._codmap is None or not isinstance(payload, dict):
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return payload
        try:
            codmap_text = self._codmap.generate()
        except Exception:
            return payload
        if not codmap_text:
            return payload
        reminder = (
            "<system-reminder>\n"
            "<codmap>\n"
            f"{codmap_text}\n"
            "</codmap>\n"
            "</system-reminder>"
        )
        for msg in reversed(messages):
            if getattr(msg, "role", None) == "user":
                existing = msg.content or ""
                msg.content = f"{existing}\n{reminder}" if existing else reminder
                break
        return payload

    async def inject_spec_acs(self, payload: Any) -> Any:
        """PR-06: Append pending acceptance criteria as a system-reminder.

        Re-loads the spec document on each call to pick up AC state
        changes since the engine started (user checks off an AC → next
        LLM call should see the updated list).
        """
        if self.spec_document is None or not isinstance(payload, dict):
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return payload
        try:
            from .spec_loader import load_spec_document
            doc = load_spec_document(self._workspace)
        except Exception:
            return payload
        active = doc.get_active_phase()
        if not active:
            return payload
        pending = active.pending_acs
        if not pending:
            return payload
        ac_lines = "\n".join(
            f"- [ ] {ac.id}: {ac.description[:80]}" for ac in pending[:5]
        )
        reminder = (
            "<system-reminder>\n"
            "<spec_acs>\n"
            f"Active phase: {active.id} {active.title}\n"
            f"Pending ACs (top 5 of {len(pending)}):\n"
            f"{ac_lines}\n"
            "</spec_acs>\n"
            "</system-reminder>"
        )
        for msg in reversed(messages):
            if getattr(msg, "role", None) == "user":
                existing = msg.content or ""
                msg.content = f"{existing}\n{reminder}" if existing else reminder
                break
        return payload

    # ── Status / introspection (for /status and tests) ──────────────

    @property
    def codmap_active(self) -> bool:
        return self._codmap is not None

    @property
    def spec_ac_inject_active(self) -> bool:
        return (
            self.spec_document is not None
            and bool(getattr(self._config, "spec_ac_inject", True))
        )