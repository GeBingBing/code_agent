"""Pytest configuration for all tests."""

import os

# Enable testing mode to relax workspace path restrictions
os.environ["CODING_AGENT_TESTING"] = "1"
# Provide a dummy API key for LLMClient construction in tests.
# Real network calls are mocked or skipped — this only satisfies openai>=2.41's
# stricter credential check at client construction time. Individual tests that
# exercise the real OpenAI client monkeypatch this as well.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-do-not-use")
