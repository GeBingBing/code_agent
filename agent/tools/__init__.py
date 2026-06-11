"""Tools - File operations, shell commands, and skill management."""

# Import all tools to register them with the global registry
from . import (
    code_search,  # noqa: F401
    file_ops,  # noqa: F401
    git_tool,  # noqa: F401
    grep,  # noqa: F401
    install,  # noqa: F401
    memory,  # noqa: F401 (PR-04: semantic_search)
    sandbox,  # noqa: F401
    shell,  # noqa: F401
    skill_manager,  # noqa: F401
    sub_agent,  # noqa: F401
    test_runner,  # noqa: F401
    web_fetch,  # noqa: F401
    web_search,  # noqa: F401
)
