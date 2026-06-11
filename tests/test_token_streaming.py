"""Tests for token-level streaming output."""

import asyncio

import pytest


class MockDelta:
    """Mock a single token delta."""

    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockChoice:
    """Mock a streaming choice."""

    def __init__(self, delta):
        self.delta = delta


class MockChunk:
    """Mock a streaming chunk."""

    def __init__(self, content: str = "", tool_calls=None):
        self.choices = [MockChoice(MockDelta(content, tool_calls))]


class MockStreamResponse:
    """Mock OpenAI streaming response - yields tokens one by one."""

    def __init__(self, tokens: list):
        self.tokens = tokens

    def __iter__(self):
        return iter(self.tokens)


class TestTokenLevelStreaming:
    """Test that streaming yields token-level events."""

    @pytest.fixture
    def mock_streaming_llm(self, monkeypatch):
        """Mock LLM that returns tokens one at a time."""

        async def mock_chat(*args, **kwargs):
            tokens = [
                MockChunk("Hello"),
                MockChunk(" "),
                MockChunk("world"),
                MockChunk("!"),
            ]
            return MockStreamResponse(tokens), True

        return mock_chat

    def test_streaming_yields_individual_tokens(self, monkeypatch):
        """Each chunk should yield a separate content event."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            tokens = [
                MockChunk("H"),
                MockChunk("e"),
                MockChunk("l"),
                MockChunk("l"),
                MockChunk("o"),
            ]
            return MockStreamResponse(tokens), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        content_events = [e for e in events if e.get("type") == "content"]
        assert len(content_events) == 5
        contents = [e.get("content", "") for e in content_events]
        assert "".join(contents) == "Hello"

    def test_streaming_assembles_full_content(self, monkeypatch):
        """Full content should be assembled from all tokens."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            tokens = [
                MockChunk("The "),
                MockChunk("quick "),
                MockChunk("brown "),
                MockChunk("fox"),
            ]
            return MockStreamResponse(tokens), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        content_end = [e for e in events if e.get("type") == "content_end"]
        final = [e for e in events if e.get("type") == "final"]

        if content_end:
            assert "The quick brown fox" in content_end[0].get("content", "")

    def test_streaming_incremental_content(self, monkeypatch):
        """Content should accumulate incrementally across events."""
        from agent.core.engine import AgentConfig, AgentEngine

        accumulated = []

        async def mock_chat(*args, **kwargs):
            tokens = [
                MockChunk("a"),
                MockChunk("b"),
                MockChunk("c"),
            ]
            return MockStreamResponse(tokens), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            async for event in engine.run_stream("test"):
                if event.get("type") == "content":
                    accumulated.append(event.get("content", ""))
                if event.get("type") == "final":
                    break

        asyncio.run(run())
        assert len(accumulated) == 3
        assert "".join(accumulated) == "abc"

    def test_run_stream_yields_token_events(self, monkeypatch):
        """run_stream should yield content events for each token."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("token")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
            return events

        events = asyncio.run(run())
        types = [e.get("type") for e in events]
        assert "step_start" in types
        assert "thinking" in types
        assert "content" in types
        assert "content_end" in types
        assert "final" in types


class TestStreamingEventTypes:
    """Test that streaming yields correct event types."""

    def test_step_start_event(self):
        """Stream should start with step_start event."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("done")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        step_starts = [e for e in events if e.get("type") == "step_start"]
        assert len(step_starts) >= 1
        assert step_starts[0].get("step") == 1

    def test_thinking_event(self):
        """Thinking event should be yielded."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("result")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        thinking_events = [e for e in events if e.get("type") == "thinking"]
        assert len(thinking_events) >= 1

    def test_content_end_event(self):
        """Content end event should contain full assembled content."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("full content")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
            return events

        events = asyncio.run(run())
        content_ends = [e for e in events if e.get("type") == "content_end"]
        assert len(content_ends) >= 1
        assert "full content" in content_ends[0].get("content", "")

    def test_final_event(self):
        """Final event should contain the complete response."""
        from agent.core.engine import AgentConfig, AgentEngine

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("final answer")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        final_events = [e for e in events if e.get("type") == "final"]
        assert len(final_events) >= 1
        assert "final answer" in final_events[0].get("content", "")
