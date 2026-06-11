# Coding Agent — Architecture

## Overview

Coding Agent is a Python CLI tool that implements a ReAct (Reason + Act) loop with tool calling, multi-layer memory, streaming output, and plan-then-execute mode. It supports 6 LLM providers (OpenAI, Kimi, MiniMax, DashScope, Zhipu, Ollama).

## Layer Diagram

```
ui/cli.py ───────────────────────────────────────────────────────────
    │  prompt_toolkit input  │  streaming output  │  confirm dialog
    ▼
agent/core/engine.py ────────────────────────────────────────────────
    │  ReAct loop  │  plan-then-execute  │  tool orchestration
    │  _execute_tool()  │  _partition_tool_calls()
    ▼
┌───────────────┬────────────────┬────────────────┬─────────────────┐
│ permissions   │ memory         │ prompts        │ config          │
│ risk assess   │ 3-layer memory │ system prompt  │ unified config  │
│ confirm flow  │ vector memory  │ sections       │ .env + json     │
└───────────────┴────────────────┴────────────────┴─────────────────┘
    │
    ▼
agent/tools/*.py ────────────────────────────────────────────────────
    │  BaseTool  │  build_tool()  │  ToolRegistry  │  30+ tools
    ▼
agent/llm/client.py ─────────────────────────────────────────────────
    │  LLMClient  │  6 providers  │  streaming + non-streaming
```

## Key Design Decisions

### 1. Plan-Then-Execute

Two-phase execution:
- **Plan phase**: Agent runs in read-only mode, explores codebase, produces `ExecutionPlan`
- **Execute phase**: Plan is injected as context, agent executes steps

The plan is NOT a pre-step — it's an LLM-driven exploration phase that can use read tools (read_file, grep, code_search).

### 2. Shared Tool Execution

`_execute_tool()` in engine.py is the single point where tools are dispatched. Every execution path (run, run_stream, run_execute) calls this method. It handles:
1. Context injection (cwd, parent_run_id)
2. Permission check (global → tool-level)
3. Confirmation (with handler or async input)
4. Execution
5. Logging and memory recording

### 3. Concurrent Tool Execution

Read-only tools marked `is_concurrency_safe = True` execute in parallel via `asyncio.gather`. Write tools serialize. A failed tool in a concurrent batch triggers sibling abort.

### 4. Build Tool Factory

`build_tool()` creates tools with safe defaults (fail-closed):
- `is_concurrency_safe = False`
- `is_read_only = False`
- `check_permissions` → allow

Each tool only overrides what it needs.

### 5. Tool-Level Permissions

1. Global `permissions.check()` — risk assessment + mode enforcement
2. Tool `check_permissions()` — tool-specific validation (e.g., shell metachar check)
3. Confirmation UI — Claude Code-style 3-option prompt

### 6. Unified Config

`agent/core/config.py` is the single config source. Priority: env var > config.json > default. `.env` loaded once at module import via `python-dotenv`.

## Data Flow

```
User input → IntentRouter (LLM classifier) → ask/edit/agent handler
    │
    ├── ask  → _direct_answer() → stream LLM response
    ├── edit → _run_edit() → engine.run_stream() (no plan)
    └── agent → _run_task() → plan phase → confirm → execute phase
```

## Concurrency Model

```
engine.run_stream()
    │
    ├── LLM response with multiple tool_calls
    ├── _partition_tool_calls()
    │   ├── concurrent (reads) → asyncio.gather()
    │   └── serial (writes) → sequential await
    └── yield results in original order
```
