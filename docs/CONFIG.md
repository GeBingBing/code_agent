# Configuration Reference

## Priority (highest to lowest)

1. **Environment variables** (`os.environ`)
2. **config.json** (`~/.coding-agent/config.json`)
3. **Default values** (hardcoded in `agent/core/config.py`)

`.env` files are loaded at startup via `python-dotenv` with `override=False` — they only set values if not already set by the environment.

## Config Keys

### Core Settings

| Key | Env Var | Default | Description |
|-----|---------|---------|-------------|
| `model` | `DEFAULT_MODEL` | `moonshot-v1-8k` | LLM model name |
| `provider` | `DEFAULT_PROVIDER` | `kimi` | LLM provider |
| `mode` | `AGENT_MODE` | `default` | Permission mode |
| `verbose` | — | `True` | Console output verbosity |

### Limits

| Key | Env Var | Default |
|-----|---------|---------|
| `max_steps` | `AGENT_MAX_STEPS` | `200` |
| `max_tokens` | `AGENT_MAX_TOKENS` | `15000` |
| `max_tool_retries` | — | `1` |

### Server

| Key | Env Var | Default |
|-----|---------|---------|
| `server_port` | `AGENT_SERVER_PORT` | `18792` |
| `server_key` | `AGENT_SERVER_KEY` | `""` |

### MCP

| Key | Env Var | Default |
|-----|---------|---------|
| `mcp_enabled` | `MCP_ENABLED` | `False` |
| `mcp_config_path` | `MCP_CONFIG_PATH` | `""` |

## Permission Modes

| Mode | LOW risk | MEDIUM | HIGH | CRITICAL |
|------|---------|--------|------|----------|
| `default` | Auto | Confirm | Confirm | Block |
| `auto` | Auto | Auto | Confirm | Block |
| `bypass` | Auto | Auto | Auto | Block |
| `plan` | Read-only | Block | Block | Block |

Set via `AGENT_MODE` env var or `--mode` CLI flag.

## API Keys

| Provider | Env Var |
|----------|---------|
| OpenAI | `OPENAI_API_KEY` |
| Kimi/Moonshot | `KIMI_API_KEY` |
| MiniMax | `MINIMAX_API_KEY` |
| DashScope | `DASHSCOPE_API_KEY` |
| Zhipu | `ZHIPU_API_KEY` |
| Ollama | `OLLAMA_BASE_URL` (no key needed) |

## Usage in Code

```python
from agent.core.config import config

model = config.get("model")           # → env or default
api_key = config.get_api_key("kimi")  # → KIMI_API_KEY
all_keys = config.get_provider_keys() # → {provider: key, ...}
```
