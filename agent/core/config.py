"""Unified configuration — single source of truth for all modules.

Load priority (highest to lowest):
  1. Environment variables (os.environ)
  2. ~/.coding-agent/config.json
  3. Default values

Usage:
    from agent.core.config import config
    model = config.get("model")  # → "moonshot-v1-8k"
"""

import json
import os
from pathlib import Path
from typing import Any, Optional


# ── Load .env file (once, at module load) ──────────────────────────

def _load_dotenv():
    """Load .env from project root using python-dotenv."""
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        return  # python-dotenv not installed — skip
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        _load(env_path, override=False)


_load_dotenv()


# ── Load config.json (once, at module load) ────────────────────────

def _load_config_json() -> dict:
    """Load ~/.coding-agent/config.json if it exists."""
    config_path = Path.home() / ".coding-agent" / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_CONFIG_JSON = _load_config_json()


# ── Default values ─────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "model": "moonshot-v1-8k",
    "provider": "kimi",
    "mode": "default",
    "max_steps": 200,
    "max_tokens": 15000,
    "max_tool_retries": 1,
    "auto_evolve": False,
    "mcp_enabled": False,
    "mcp_config_path": "",
    "server_port": 18792,
    "server_key": "",
    "verbose": True,
    "workspace": "",
    # PR-04: embedding provider
    "embedding_provider": "auto",  # "auto" | "sentence-transformers" | "tfidf" | "hashing"
    "embedding_model": "all-MiniLM-L6-v2",
    "embedding_dim": 128,
    # PR-14: user profile (root-cause fix for session amnesia)
    "user_profile_enabled": True,    # Master switch for the user profile system
    "auto_remember_user_facts": True,  # Auto-extract identity from user messages
    "memory_pinned_max": 200,        # Cap for pinned entries in memory.md
    "user_profile_path": "",         # Override default ~/.coding-agent/user_profile.json
    # PR-15: LLM-based extractors (replacing hard-coded regex lists)
    "intent_use_llm": True,          # Use LLM for IntentClassifier (else legacy heuristic)
    "fact_extraction_use_llm": True, # Use LLM for FactExtractor (else PR-14 regex)
}


# ── Environment variable mapping ───────────────────────────────────

_ENV_MAP = {
    "model": "DEFAULT_MODEL",
    "provider": "DEFAULT_PROVIDER",
    "mode": "AGENT_MODE",
    "max_steps": "AGENT_MAX_STEPS",
    "max_tokens": "AGENT_MAX_TOKENS",
    "server_port": "AGENT_SERVER_PORT",
    "server_key": "AGENT_SERVER_KEY",
    "workspace": "CODING_AGENT_WORKSPACE",
    "mcp_enabled": "MCP_ENABLED",
    "mcp_config_path": "MCP_CONFIG_PATH",
    # PR-04
    "embedding_provider": "EMBEDDING_PROVIDER",
    "embedding_model": "EMBEDDING_MODEL",
    "embedding_dim": "EMBEDDING_DIM",
    # PR-14
    "user_profile_enabled": "AGENT_USER_PROFILE",
    "auto_remember_user_facts": "AGENT_AUTO_REMEMBER",
    "memory_pinned_max": "AGENT_MEMORY_PINNED_MAX",
    "user_profile_path": "CODING_AGENT_USER_PROFILE",
    # PR-15
    "intent_use_llm": "AGENT_INTENT_USE_LLM",
    "fact_extraction_use_llm": "AGENT_FACT_USE_LLM",
}


# ── API Keys (look up by provider name) ────────────────────────────

_PROVIDER_KEY_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "kimi": "KIMI_API_KEY",
    "moonshot": "KIMI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "ollama": None,  # local — no key needed
}


# ── Config singleton ────────────────────────────────────────────────

class Config:
    """Unified configuration accessor."""

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value with env > config.json > default priority.

        For API keys, use get_api_key(provider) instead.
        """
        env_name = _ENV_MAP.get(key)
        if env_name and env_name in os.environ:
            val = os.environ[env_name]
            # Coerce bool and int types
            if key in ("mcp_enabled", "auto_evolve", "verbose",
                       "user_profile_enabled", "auto_remember_user_facts",
                       "intent_use_llm", "fact_extraction_use_llm"):
                return val.lower() in ("1", "true", "yes")
            if key in ("max_steps", "max_tokens", "max_tool_retries", "server_port",
                       "memory_pinned_max", "embedding_dim"):
                try:
                    return int(val)
                except ValueError:
                    pass
            return val
        if key in _CONFIG_JSON:
            return _CONFIG_JSON[key]
        if default is not None:
            return default
        return _DEFAULTS.get(key)

    def get_api_key(self, provider: str) -> Optional[str]:
        """Get API key for a given provider."""
        env_name = _PROVIDER_KEY_ENV_MAP.get(provider)
        if env_name:
            return os.environ.get(env_name)
        return None

    def get_base_url(self, provider: str) -> Optional[str]:
        """Get custom base URL for a provider (e.g. OLLAMA_BASE_URL)."""
        env_name = f"{provider.upper()}_BASE_URL"
        return os.environ.get(env_name)

    def get_all(self) -> dict:
        """Return all known config values (for debugging/diagnostic)."""
        result = {}
        for key in _DEFAULTS:
            result[key] = self.get(key)
        return result

    def get_provider_keys(self) -> dict:
        """Return all available API key → provider mappings."""
        result = {}
        for provider, env_name in _PROVIDER_KEY_ENV_MAP.items():
            if env_name and env_name in os.environ:
                result[provider] = os.environ[env_name]
        return result


# Global singleton
config = Config()
