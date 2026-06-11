"""StructuredOutput tool — enforce JSON output format for the next response."""

import json

from .base import BaseTool, ToolResult, registry


class StructuredOutputTool(BaseTool):
    """Tell the LLM to output JSON in the next response.

    This is a meta-tool: calling it doesn't execute anything — it returns
    instructions that guide the LLM to format its next text response as JSON.
    The schema parameter defines the expected JSON structure.
    """

    user_facing_name = "Schema"
    is_concurrency_safe = True
    is_read_only = True  # Doesn't modify anything

    name = "structured_output"
    description = (
        "Request the next response to be formatted as JSON matching a schema. "
        "Use this when you need to produce structured data (lists, configs, "
        "API payloads). The schema parameter describes the expected JSON shape. "
        "After calling this tool, your NEXT text response MUST be valid JSON "
        "matching the schema."
    )

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schema": {
                            "type": "string",
                            "description": (
                                "JSON schema describing the expected output. "
                                'Example: \'{"type": "array", "items": {"name": "string", "version": "string"}}\''
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Human-readable description of what the JSON should contain",
                        },
                    },
                    "required": ["schema"],
                },
            },
        }

    def render_call(self, args: dict) -> str:
        desc = args.get("description", "")[:50]
        return f"Schema · {desc}" if desc else "Schema"

    async def execute(self, schema: str, description: str = "", **kwargs) -> ToolResult:
        try:
            # Validate schema is valid JSON
            parsed = json.loads(schema)
        except json.JSONDecodeError as e:
            return ToolResult(success=False, content="", error=f"Invalid schema JSON: {e}")

        instruction = (
            "STRUCTURED OUTPUT REQUIRED\n"
            f"Description: {description or 'JSON output'}\n"
            f"Schema: {json.dumps(parsed)}\n\n"
            "Your NEXT response must be ONLY valid JSON matching this schema. "
            "No markdown, no explanation, no code fences — just the JSON object. "
            "Wrap the output in ```json ... ``` fences so it renders properly."
        )

        return ToolResult(
            success=True,
            content=instruction,
            metadata={"schema_type": parsed.get("type", "object")},
        )


registry.register(StructuredOutputTool())
