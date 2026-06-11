"""Agent role definitions (PR-07).

Each `AgentRole` is a declarative profile that the orchestrator uses to
configure a sub-agent:
- `system_prompt_addon`: instructions prepended to the sub-agent's system prompt
- `tools`: which tool names the sub-agent is allowed to call
- `preferred_model`: optional model override (e.g., a smaller model for review)

Roles are kept simple on purpose: the orchestrator does the heavy lifting
(decomposition, scheduling, merging), and the sub-agents are just specialized
LLM invocations with a different system prompt + tool allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class AgentRole:
    """Declarative role profile for a sub-agent."""
    name: str
    description: str
    system_prompt_addon: str
    tools: List[str] = field(default_factory=list)
    preferred_model: Optional[str] = None
    is_read_only: bool = False  # review-style roles do not mutate state

    def with_tools(self, *extra: str) -> "AgentRole":
        """Return a new role with extra tools added (immutability preserved)."""
        merged = list(self.tools) + [t for t in extra if t not in self.tools]
        return AgentRole(
            name=self.name,
            description=self.description,
            system_prompt_addon=self.system_prompt_addon,
            tools=merged,
            preferred_model=self.preferred_model,
            is_read_only=self.is_read_only,
        )


# ── Built-in roles (1.md §7.1) ────────────────────────────────────


CODE_ROLE = AgentRole(
    name="code_generator",
    description="编码专家，专注于实现功能。",
    system_prompt_addon=(
        "You are a code generator specialist. Your job is to implement features "
        "based on specifications. Focus on:\n"
        "- Clean, idiomatic code\n"
        "- Proper error handling\n"
        "- Following project conventions (see CODING_AGENT.md)\n"
        "- Writing minimal, focused changes\n\n"
        "You MUST NOT modify tests, docs, or config files. Only code."
    ),
    tools=["read_file", "write_file", "apply_diff", "list_files", "code_search"],
)


TEST_ROLE = AgentRole(
    name="test_engineer",
    description="测试专家，编写单元/集成测试。",
    system_prompt_addon=(
        "You are a test engineer. Your job is to write comprehensive tests.\n"
        "Focus on:\n"
        "- Edge cases\n"
        "- Failure modes\n"
        "- Coverage of acceptance criteria\n"
        "- Following TDD (Red → Green → Refactor)\n\n"
        "Use `run_tests` to verify your tests actually pass."
    ),
    tools=["read_file", "write_file", "run_tests", "code_search", "grep"],
)


REVIEWER_ROLE = AgentRole(
    name="reviewer",
    description="代码审查专家，专注架构合规性。",
    system_prompt_addon=(
        "You are a code reviewer. Your job is to review changes for:\n"
        "- Architecture compliance\n"
        "- Security vulnerabilities\n"
        "- Performance issues\n"
        "- Convention violations (PEP 8, project style)\n\n"
        "You do NOT modify code — only report findings. "
        "Use `read_file` and `grep` to inspect the diff and surrounding code."
    ),
    tools=["read_file", "grep", "code_search"],
    is_read_only=True,
)


DEVOPS_ROLE = AgentRole(
    name="devops",
    description="DevOps 专家，环境配置 + CI 交互。",
    system_prompt_addon=(
        "You are a DevOps specialist. Your job is to:\n"
        "- Configure environments (.env, requirements.txt, Dockerfile)\n"
        "- Set up CI pipelines\n"
        "- Manage deployments\n"
        "- Handle git operations (commit, branch, PR)\n\n"
        "Use `execute_command` carefully — high-risk operations need user "
        "confirmation. Always check `git status` before destructive actions."
    ),
    tools=["read_file", "write_file", "execute_command", "git_status", "git_commit"],
)


BUILTIN_ROLES: Dict[str, AgentRole] = {
    "code": CODE_ROLE,
    "test": TEST_ROLE,
    "reviewer": REVIEWER_ROLE,
    "devops": DEVOPS_ROLE,
}


def get_role(name: str) -> AgentRole:
    """Look up a built-in role by short name (e.g. 'code' → CODE_ROLE)."""
    if name in BUILTIN_ROLES:
        return BUILTIN_ROLES[name]
    raise KeyError(
        f"Unknown role: {name!r}. Built-in roles: {list(BUILTIN_ROLES.keys())}"
    )
