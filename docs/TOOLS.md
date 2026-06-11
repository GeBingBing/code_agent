# Tool Development Guide

## Creating a Tool

### Via build_tool() (Recommended)

```python
from agent.tools.base import build_tool, ToolResult, registry

async def my_execute(text: str = "", **kwargs) -> ToolResult:
    return ToolResult(success=True, content=f"Processed: {text}")

tool = build_tool(
    name="my_tool",
    description="Processes text input",
    execute_fn=my_execute,
    is_read_only=True,
    schema_override={
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "Processes text input",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to process"},
                },
                "required": ["text"],
            },
        },
    },
)
registry.register(tool)
```

### Via BaseTool Subclass

```python
from agent.tools.base import BaseTool, ToolResult, registry

class MyTool(BaseTool):
    name = "my_tool"
    description = "Processes text input"
    is_read_only = True

    @property
    def schema(self):
        return {
            "type": "function",
            "function": { ... }
        }

    async def execute(self, text: str = "", **kwargs) -> ToolResult:
        return ToolResult(success=True, content=f"Processed: {text}")

registry.register(MyTool())
```

## BaseTool Properties

| Property | Default | Description |
|----------|---------|-------------|
| `is_concurrency_safe` | `False` | Can run in parallel with other tools |
| `is_read_only` | `False` | Does not modify files or system state |

## BaseTool Methods

| Method | Default | Description |
|--------|---------|-------------|
| `execute(**kwargs) → ToolResult` | abstract | Tool logic (required) |
| `check_permissions(args) → (bool, str)` | allow | Tool-specific validation |
| `render_call(args) → str` | generic | CLI display for tool use |
| `render_result(result) → str` | generic | CLI display for tool result |
| `prompt_contribution() → str` | "" | Added to system prompt |

## ToolResult

```python
@dataclass
class ToolResult:
    success: bool        # True if tool executed successfully
    content: str         # Output content
    error: Optional[str] # Error message if success=False
```

## Registration

Tools self-register via module import:
```python
# In agent/tools/__init__.py
from . import my_tool  # Triggers registry.register()
```

Or register explicitly:
```python
registry.register(my_tool_instance)
```
