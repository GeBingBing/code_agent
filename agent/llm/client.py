"""LLM Client - Supports OpenAI, DashScope (Alibaba), Ollama, and more."""

from dataclasses import dataclass
from typing import List, Optional

from ..core.config import config


@dataclass
class Message:
    role: str
    content: str
    tool_call_id: Optional[str] = None


class LLMClient:
    """LLM client with multi-provider support"""

    PROVIDERS = {
        "openai": "https://api.openai.com/v1",
        "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "minimax": "https://api.minimax.chat/v1",
        "kimi": "https://api.moonshot.cn/v1",
    }

    def __init__(
        self,
        model: str = None,
        api_key: Optional[str] = None,
        provider: str = None,
        base_url: Optional[str] = None,
    ):
        model = model or config.get("model", "qwen-plus")
        provider = provider or config.get("provider", "auto")
        """
        Args:
            model: Model name (e.g., "gpt-4o", "qwen-plus", "qwen-turbo")
            api_key: API key (falls back to env var)
            provider: "openai", "dashscope", "ollama", or "auto" (detect from model)
            base_url: Custom base URL
        """
        self.model = model
        self.provider = self._detect_provider(provider, model)

        # Get API key based on provider
        if api_key:
            self.api_key = api_key
        else:
            self.api_key = config.get_api_key(self.provider) or config.get_api_key("openai")

        if not self.api_key and self.provider not in ("ollama", "mock"):
            raise ValueError(
                f"No API key found for provider '{self.provider}'. "
                "Set the API key in .env file or environment."
            )

        # Determine base URL
        if base_url:
            self.base_url = base_url
        elif self.provider in ("minimax", "ollama"):
            self.base_url = config.get_base_url(self.provider) or self.PROVIDERS.get(
                self.provider, self.PROVIDERS["openai"]
            )
        else:
            self.base_url = self.PROVIDERS.get(self.provider, self.PROVIDERS["openai"])

        # Create client
        if self.provider == "ollama":
            try:
                from openai import OpenAI

                self.client = OpenAI(
                    api_key="ollama",
                    base_url=self.base_url,
                )
            except ImportError as err:
                raise ImportError("Please install openai: pip install openai") from err
        elif self.provider == "mock":
            # Mock provider: no real API calls; openai>=2.41 refuses api_key=None.
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key="mock", base_url=self.base_url)
            except ImportError as err:
                raise ImportError("Please install openai: pip install openai") from err
        else:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except ImportError as err:
                raise ImportError("Please install openai: pip install openai") from err

    def _detect_provider(self, provider: str, model: str) -> str:
        """Auto-detect provider from model name"""
        if provider != "auto":
            return provider

        model_lower = model.lower()

        # MiniMax models
        if "minimax" in model_lower or "abab" in model_lower:
            return "minimax"

        # Zhipu/GLM models
        if any(m in model_lower for m in ["glm", "chatglm"]):
            return "zhipu"

        # Ollama models — check BEFORE dashscope to avoid llama → dashscope false positive
        if "/" in model or model_lower.startswith("llama") or model_lower.startswith("codellama"):
            return "ollama"

        # DashScope models (qwen/* family, NOT llama which is handled above)
        if any(m in model_lower for m in ["qwen", "qwq", "baichuan", "wanx"]):
            return "dashscope"

        # Kimi / Moonshot models
        if any(m in model_lower for m in ["moonshot", "kimi"]):
            return "kimi"

        return "openai"

    async def chat(
        self,
        messages: List[Message],
        tools: Optional[List[dict]] = None,
        stream: bool = False,
        **kwargs,
    ):
        """Send a chat request"""
        import json

        msg_dicts = []
        system_content = ""

        for m in messages:
            if m.role == "system":
                # DashScope/Qwen and some other providers reject system role.
                # Merge all system messages into the first user message.
                system_content += m.content + "\n\n"
                continue
            d = {"role": m.role, "content": m.content}
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            if getattr(m, "tool_calls", None):
                d["tool_calls"] = json.loads(m.tool_calls)
                if not d["content"]:
                    d["content"] = None
            msg_dicts.append(d)

        # Merge collected system content into first user message
        if system_content and msg_dicts:
            for i, d in enumerate(msg_dicts):
                if d["role"] == "user":
                    d["content"] = system_content.strip() + "\n\n" + (d["content"] or "")
                    break

        params = {
            "model": self.model,
            "messages": msg_dicts,
            **kwargs,
        }

        if tools:
            params["tools"] = tools

        if stream:
            params["stream"] = True
            # Yield raw chunks with metadata so engine can filter thinking tags
            return self.client.chat.completions.create(**params), True
        else:
            response = self.client.chat.completions.create(**params)
            message = response.choices[0].message
            # Return the full message object if it contains tool calls,
            # so the engine can process them. Otherwise return content string.
            if getattr(message, "tool_calls", None):
                return message
            # Some models (Ollama qwen3, DashScope qwen3.6-plus) return reasoning
            # in separate fields when content is empty
            content = message.content or ""
            if not content:
                if hasattr(message, "reasoning"):
                    content = message.reasoning
                elif hasattr(message, "reasoning_content"):
                    content = message.reasoning_content
            return content


def count_tokens(text: str) -> int:
    """Rough token estimation"""
    return len(text) // 4


# ── P14-2: Alternate provider factory (cross-family dual-agent) ──


# Family taxonomy used to enforce cross-family strict matching:
# when strict_cross_provider=True, secondary MUST come from a different family.
_FAMILY_OF_PROVIDER = {
    "openai": "openai",
    "kimi": "kimi",
    "moonshot": "kimi",
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "zhipu": "zhipu",
    "glm": "zhipu",
    "minimax": "minimax",
    "abab": "minimax",
    "ollama": "ollama",
}


def _detect_family(provider: str, model) -> str:
    """Return the family name for a provider/model pair.

    Falls back to the provider name if model is empty/unknown. Used to enforce
    cross-family strict matching in dual-review.
    """
    p = (provider or "").lower()
    if p in _FAMILY_OF_PROVIDER:
        return _FAMILY_OF_PROVIDER[p]
    m = (model or "").lower()
    for token, fam in _FAMILY_OF_PROVIDER.items():
        if token in m:
            return fam
    return p or "unknown"


def _pick_alternate_provider_name(primary_provider: str, primary_model: str):
    """Pick a provider name that is in a DIFFERENT family from the primary.

    Strategy (industry-standard preference order):
    1. Honor explicit ``DUAL_REVIEW_PROVIDER`` env var if set and family differs.
    2. Otherwise, prefer the highest-priority different-family provider for which
       an API key is available in the environment.
    3. Return None if no other family has an available key.

    Priority (cross-family preference order): openai → dashscope → zhipu → minimax → kimi
    This matches what humans actually configure (e.g. OpenAI primary, DashScope
    secondary is a very common pattern in CN teams).
    """
    import logging as _logging
    import os

    explicit = os.getenv("DUAL_REVIEW_PROVIDER")
    if explicit:
        explicit_lower = explicit.lower()
        if _detect_family(explicit_lower, "") != _detect_family(primary_provider, primary_model):
            return explicit_lower
        # Explicit but same family — warn and fall through
        _logging.getLogger(__name__).warning(
            "DUAL_REVIEW_PROVIDER=%s is same-family as primary %s; ignoring.",
            explicit,
            primary_provider,
        )

    primary_family = _detect_family(primary_provider, primary_model)
    # Preference order — pick the first provider with a key and a different family.
    for cand in ("openai", "dashscope", "zhipu", "minimax", "kimi"):
        if cand == primary_provider:
            continue
        if _detect_family(cand, "") == primary_family:
            continue
        if config.get_api_key(cand):
            return cand
    return None


def create_alternate_provider_client(primary_client):
    """Construct a SECOND LLMClient from a different provider family.

    Used by the dual-agent review manager to honor the P14-2 spec:
    "复审 Agent 来自不同 provider (OpenAI + Anthropic 互审)".

    Returns None when:
    - Only one provider has an API key in the environment (single-provider users
      see no effect — no error, no fallback to same family).
    - No alternate provider can be selected.

    The returned client is fully constructed (constructor ran), so callers can
    use it exactly like ``primary_client``. Selection of an appropriate model
    name for the alternate family follows sensible defaults.
    """
    import logging as _logging
    import os

    primary_provider = primary_client.provider if primary_client else None
    primary_model = primary_client.model if primary_client else None

    alt_provider = _pick_alternate_provider_name(primary_provider or "openai", primary_model or "")
    if alt_provider is None:
        return None

    # Default model name per family
    family_defaults = {
        "openai": "gpt-4o-mini",
        "dashscope": "qwen-plus",
        "zhipu": "glm-4-plus",
        "minimax": "abab6.5s-chat",
        "kimi": "moonshot-v1-8k",
    }
    alt_family = _detect_family(alt_provider, "")
    alt_model = family_defaults.get(alt_family, "")

    env_model = os.getenv("DUAL_REVIEW_MODEL")
    if env_model:
        alt_model = env_model

    try:
        new_client = LLMClient(model=alt_model, provider=alt_provider)
        _logging.getLogger(__name__).info(
            "P14-2 dual-review: secondary client ready (provider=%s, model=%s)",
            new_client.provider,
            new_client.model,
        )
        return new_client
    except Exception as e:
        _logging.getLogger(__name__).warning(
            "P14-2 dual-review: failed to construct alternate client (%s) — "
            "falling back to single-client mode",
            e,
        )
        return None
