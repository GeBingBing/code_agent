"""Pytest configuration for all tests."""

import os

import pytest

# Enable testing mode to relax workspace path restrictions
os.environ["CODING_AGENT_TESTING"] = "1"
# Provide a dummy API key for LLMClient construction in tests.
# Real network calls are mocked or skipped — this only satisfies openai>=2.41's
# stricter credential check at client construction time. Individual tests that
# exercise the real OpenAI client monkeypatch this as well.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-do-not-use")


# ── Mock LLM fixtures (RES-002) ─────────────────────────────────────
# Used by tests/contracts/test_mock_llm_contract.py. Provides a fresh
# _MockLLMBackend per test (no cross-test pollution) and a factory for
# building scripted assistant messages with tool_calls.


@pytest.fixture
def mock_llm():
    """Fresh in-process _MockLLMBackend — no network, no shared state."""
    from agent.llm.client import _MockLLMBackend

    return _MockLLMBackend()


@pytest.fixture
def mock_message_factory():
    """Factory for building _MockMessage objects for queue_response()."""
    from agent.llm.client import _MockMessageFactory

    return _MockMessageFactory()
