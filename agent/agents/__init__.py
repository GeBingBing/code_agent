"""Multi-agent primitives (PR-07).

Provides:
- `AgentRole` — declarative role definition (system prompt addon, tools, model)
- Built-in roles: `CODE_ROLE`, `TEST_ROLE`, `REVIEWER_ROLE`, `DEVOPS_ROLE`
- `TaskRequest` / `TaskResponse` — inter-agent task protocol
- `OrchestratorAgent` — PM role that decomposes a task into a DAG of
  subtasks and dispatches them via EventBus
"""

from .roles import (
    AgentRole,
    CODE_ROLE,
    TEST_ROLE,
    REVIEWER_ROLE,
    DEVOPS_ROLE,
    BUILTIN_ROLES,
    get_role,
)
from .orchestrator import (
    OrchestratorAgent,
    TaskRequest,
    TaskResponse,
    TaskExecutionError,
    CyclicDependencyError,
)
from .evaluator import (
    EvaluatorAgent,
    EvaluationScore,
    EvaluationReport,
    DIMENSIONS,
)

__all__ = [
    "AgentRole",
    "CODE_ROLE", "TEST_ROLE", "REVIEWER_ROLE", "DEVOPS_ROLE",
    "BUILTIN_ROLES", "get_role",
    "OrchestratorAgent",
    "TaskRequest", "TaskResponse",
    "TaskExecutionError", "CyclicDependencyError",
    "EvaluatorAgent", "EvaluationScore", "EvaluationReport", "DIMENSIONS",
]
