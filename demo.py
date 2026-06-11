"""Demo: Full ReAct loop with mock LLM — no API key needed."""

import asyncio
import json
from pathlib import Path
from dataclasses import dataclass

from agent.core.engine import AgentEngine, AgentConfig
from agent.llm.client import Message
from agent.tools.base import registry, ToolResult
from agent.prompts.assembler import PromptAssembler


WORKSPACE = Path(__file__).parent / "workspace"


@dataclass
class MockFunction:
    name: str
    arguments: str


@dataclass
class MockToolCall:
    id: str
    function: MockFunction


@dataclass
class MockMessage:
    content: str = ""
    tool_calls: list = None


class DemoEngine(AgentEngine):
    """AgentEngine with verbose step logging."""

    async def run(self, task: str) -> str:
        skill_prompt = self.skills.activate_skills(task)
        system = PromptAssembler.build_system_prompt(
            long_term_memory=self.memory.get_long_term_context(),
            skill_prompt=skill_prompt,
        )

        self.memory.add("system", system)
        self.memory.add("user", task)

        for step in range(self.config.max_steps):
            print(f"\n{'─' * 60}")
            print(f"  Step {step + 1} / {self.config.max_steps}")
            print(f"{'─' * 60}")

            mem_messages = self.memory.get_messages()
            messages = [Message(role=m.role, content=m.content, tool_call_id=m.tool_call_id) for m in mem_messages]

            print("  [Think]  Calling LLM with current context...")
            response = await self.llm.chat(
                messages=messages,
                tools=registry.schemas
            )

            if isinstance(response, str):
                print(f"  [Final]  {response[:200]}...")
                self.memory.add("assistant", response)
                return response

            if hasattr(response, 'tool_calls') and response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

                    print(f"  [Act]    Tool: {tool_name}")
                    for k, v in args.items():
                        v_short = str(v)[:80].replace('\n', '\\n')
                        print(f"           arg {k}={v_short}")

                    if tool_name == "execute_command" and "cwd" not in args:
                        args["cwd"] = str(WORKSPACE)

                    allowed, reason = self.permissions.check(tool_name, args)
                    if not allowed:
                        print(f"  [Block]  {reason}")
                        self.memory.add("tool", f"Blocked: {reason}", tool_call_id=tool_call.id)
                        continue

                    tool = registry.get(tool_name)
                    if not tool:
                        result = ToolResult(success=False, content="", error=f"Unknown tool: {tool_name}")
                    else:
                        result = await tool.execute(**args)

                    obs = result.content if result.success else f"Error: {result.error}"
                    obs_short = str(obs)[:150].replace('\n', '\\n')
                    print(f"  [Observe] {obs_short}")

                    self.memory.add("assistant", f"Called {tool_name}")
                    self.memory.add("tool", obs, tool_call_id=tool_call.id)
            else:
                content = response.content if hasattr(response, 'content') else str(response)
                print(f"  [Final]  {content[:200]}...")
                self.memory.add("assistant", content)
                return content

        return "Max steps reached without completion"


class MockLLMClient:
    """Mock LLM that follows a scripted ReAct conversation."""

    def __init__(self, steps):
        self.steps = steps
        self.idx = 0

    async def chat(self, messages, tools=None, stream=False, **kwargs):
        if self.idx >= len(self.steps):
            return "Task completed."
        step = self.steps[self.idx]
        self.idx += 1
        return step


async def main():
    print("=" * 60)
    print("Coding Agent — Full ReAct Demo (Mock LLM)")
    print("=" * 60)
    task = "用 Python 写一个斐波那契函数，保存到 fibonacci.py，然后运行它"
    print(f"Task: {task}\n")

    steps = [
        MockMessage(
            content="",
            tool_calls=[
                MockToolCall(
                    id="call_1",
                    function=MockFunction(
                        name="write_file",
                        arguments=json.dumps({
                            "path": str(WORKSPACE / "fibonacci.py"),
                            "content": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\n\nif __name__ == '__main__':\n    for i in range(10):\n        print(f'fib({i}) = {fibonacci(i)}')\n"
                        })
                    )
                )
            ]
        ),
        MockMessage(
            content="",
            tool_calls=[
                MockToolCall(
                    id="call_2",
                    function=MockFunction(
                        name="execute_command",
                        arguments=json.dumps({
                            "command": f"cd {WORKSPACE} && python fibonacci.py",
                            "cwd": str(WORKSPACE)
                        })
                    )
                )
            ]
        ),
        MockMessage(
            content="任务完成！我已经将斐波那契函数保存到 workspace/fibonacci.py，并运行成功。输出显示了 fib(0) 到 fib(9) 的结果。",
            tool_calls=None
        ),
    ]

    config = AgentConfig(model="mock", provider="mock", mode="bypass")
    agent = DemoEngine(config)
    agent.llm = MockLLMClient(steps)

    result = await agent.run(task)

    print(f"\n{'=' * 60}")
    print("Final Result")
    print(f"{'=' * 60}")
    print(result)

    fib_file = WORKSPACE / "fibonacci.py"
    if fib_file.exists():
        print(f"\n{'=' * 60}")
        print(f"Artifact: {fib_file}")
        print(f"{'=' * 60}")
        print(fib_file.read_text())


if __name__ == "__main__":
    asyncio.run(main())
