"""Multi-agent primitives (PR-07).

Provides:
- `AgentRole` — declarative role definition (system prompt addon, tools, model)
- Built-in roles: `CODE_ROLE`, `TEST_ROLE`, `REVIEWER_ROLE`, `DEVOPS_ROLE`
- `TaskRequest` / `TaskResponse` — inter-agent task protocol
- `OrchestratorAgent` — PM role that decomposes a task into a DAG of
  subtasks and dispatches them via EventBus
"""

from .evaluator import (
    DIMENSIONS,
    EvaluationReport,
    EvaluationScore,
    EvaluatorAgent,
)
from .orchestrator import (
    CyclicDependencyError,
    OrchestratorAgent,
    TaskExecutionError,
    TaskRequest,
    TaskResponse,
)
from .roles import (
    BUILTIN_ROLES,
    CODE_ROLE,
    DEVOPS_ROLE,
    REVIEWER_ROLE,
    TEST_ROLE,
    AgentRole,
    get_role,
)

__all__ = [
    "AgentRole",
    "CODE_ROLE",
    "TEST_ROLE",
    "REVIEWER_ROLE",
    "DEVOPS_ROLE",
    "BUILTIN_ROLES",
    "get_role",
    "OrchestratorAgent",
    "TaskRequest",
    "TaskResponse",
    "TaskExecutionError",
    "CyclicDependencyError",
    "EvaluatorAgent",
    "EvaluationScore",
    "EvaluationReport",
    "DIMENSIONS",
]
