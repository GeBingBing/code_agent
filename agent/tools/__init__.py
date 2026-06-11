"""Tools - File operations, shell commands, and skill management."""

# Import all tools to register them with the global registry
from . import file_ops  # noqa: F401
from . import shell  # noqa: F401
from . import skill_manager  # noqa: F401
from . import code_search  # noqa: F401
from . import sub_agent  # noqa: F401
from . import sandbox  # noqa: F401
from . import git_tool  # noqa: F401
from . import grep  # noqa: F401
from . import web_fetch  # noqa: F401
from . import web_search  # noqa: F401
from . import test_runner  # noqa: F401
from . import install  # noqa: F401
from . import memory  # noqa: F401 (PR-04: semantic_search)