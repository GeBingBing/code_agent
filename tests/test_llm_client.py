"""Tests for Phase 0: LLM client multi-provider detection."""

import os

import pytest

from agent.llm.client import LLMClient


class TestDetectProvider:
    """Test provider auto-detection from model names."""

    def test_detect_minimax(self):
        c = LLMClient(model="MiniMax-M2.7", provider="auto")
        assert c.provider == "minimax"

    def test_detect_zhipu_glm(self):
        c = LLMClient(model="glm-5.1", provider="auto", api_key="test")
        assert c.provider == "zhipu"

    def test_detect_dashscope_qwen(self):
        c = LLMClient(model="qwen-plus", provider="auto", api_key="test")
        assert c.provider == "dashscope"

    def test_detect_kimi(self):
        c = LLMClient(model="moonshot-v1-8k", provider="auto", api_key="test")
        assert c.provider == "kimi"

    def test_explicit_provider_override(self):
        c = LLMClient(model="gpt-4o", provider="openai", api_key="test")
        assert c.provider == "openai"


class TestEnvDefaults:
    """Test reading defaults from environment variables."""

    def test_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_MODEL", "qwen-turbo")
        monkeypatch.setenv("DEFAULT_PROVIDER", "dashscope")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
        c = LLMClient()
        assert c.model == "qwen-turbo"
        assert c.provider == "dashscope"
