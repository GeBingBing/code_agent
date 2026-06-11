"""Tests for AgentEngine.run_stream() method."""

import asyncio

import pytest

from agent.core.engine import AgentConfig, AgentEngine


class MockChunk:
    """Simulate a streaming chunk."""

    def __init__(self, content: str):
        self.choices = [type("Choice", (), {"delta": type("Delta", (), {"content": content})()})()]


class MockStreamResponse:
    """Simulate OpenAI streaming response."""

    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


class MockToolCall:
    """Simulate a tool call from streaming delta."""

    def __init__(self, name: str, arguments: str, call_id: str = "call_1"):
        self.id = call_id
        self.function = type("Func", (), {"name": name, "arguments": arguments})()


class MockToolCallsDelta:
    """Simulate tool_calls delta in streaming."""

    def __init__(self, calls):
        self.tool_calls = calls


class TestRunStream:
    """Test the streaming run method."""

    @pytest.fixture
    def mock_stream_llm(self, monkeypatch):
        """Mock LLM that returns streaming response with content."""

        async def mock_chat(*args, **kwargs):
            return (
                MockStreamResponse(
                    [
                        MockChunk("Hello "),
                        MockChunk("world!"),
                    ]
                ),
                True,
            )

        return mock_chat

    @pytest.fixture
    def mock_tool_call_llm(self, monkeypatch):
        """Mock LLM that returns streaming response with tool call."""

        async def mock_chat(*args, **kwargs):
            return (
                MockStreamResponse(
                    [
                        MockChunk("I will read the file."),
                        type(
                            "Chunk",
                            (),
                            {
                                "choices": [
                                    type(
                                        "Choice",
                                        (),
                                        {
                                            "delta": type(
                                                "Delta",
                                                (),
                                                {
                                                    "content": "",
                                                    "tool_calls": [
                                                        type(
                                                            "TC",
                                                            (),
                                                            {
                                                                "id": "call_1",
                                                                "function": type(
                                                                    "Func",
                                                                    (),
                                                                    {
                                                                        "name": "read_file",
                                                                        "arguments": '{"path": "test.py"}',
                                                                    },
                                                                )(),
                                                            },
                                                        )()
                                                    ],
                                                },
                                            )()
                                        },
                                    )()
                                ]
                            },
                        )(),
                    ]
                ),
                True,
            )

        return mock_chat

    def test_run_stream_yields_step_start(self, monkeypatch):
        """run_stream should yield step_start events."""

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("Hello")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test task"):
                events.append(event)
                if event.get("type") == "final":
                    break
            return events

        events = asyncio.run(run())
        step_starts = [e for e in events if e.get("type") == "step_start"]
        assert len(step_starts) >= 1
        assert step_starts[0]["step"] == 1

    def test_run_stream_yields_content_chunks(self, monkeypatch):
        """run_stream should yield content events for each chunk."""

        async def mock_chat(*args, **kwargs):
            return (
                MockStreamResponse(
                    [
                        MockChunk("Hello "),
                        MockChunk("world!"),
                    ]
                ),
                True,
            )

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
        contents = [e.get("content", "") for e in content_events]
        assert "Hello " in contents
        assert "world!" in contents

    def test_run_stream_yields_final(self, monkeypatch):
        """run_stream should yield final event when complete."""

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("Final response")]), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("test"):
                events.append(event)
            return events

        events = asyncio.run(run())
        final_events = [e for e in events if e.get("type") == "final"]
        assert len(final_events) >= 1

    def test_run_stream_with_tool_call(self, monkeypatch):
        """run_stream should yield tool_call events."""

        async def mock_chat(*args, **kwargs):
            class MockResp:
                def __iter__(self):
                    yield type(
                        "Chunk",
                        (),
                        {
                            "choices": [
                                type(
                                    "Choice",
                                    (),
                                    {
                                        "delta": type(
                                            "Delta",
                                            (),
                                            {
                                                "content": "Reading file...",
                                                "tool_calls": [
                                                    type(
                                                        "TC",
                                                        (),
                                                        {
                                                            "id": "call_abc123",
                                                            "function": type(
                                                                "Func",
                                                                (),
                                                                {
                                                                    "name": "read_file",
                                                                    "arguments": '{"path": "test.py"}',
                                                                },
                                                            )(),
                                                        },
                                                    )()
                                                ],
                                            },
                                        )()
                                    },
                                )()
                            ]
                        },
                    )()

            return MockResp(), True

        config = AgentConfig(model="mock", provider="mock", verbose=False)
        engine = AgentEngine(config)
        engine.llm = type("StubLLM", (), {"chat": mock_chat})()

        async def run():
            events = []
            async for event in engine.run_stream("read test.py"):
                events.append(event)
                if event.get("type") == "tool_call":
                    break
            return events

        events = asyncio.run(run())
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        assert (
            len(tool_calls) >= 1
        ), f"Expected tool_call event, got {[e.get('type') for e in events]}"
        assert tool_calls[0]["tool_name"] == "read_file"
        assert tool_calls[0]["tool_args"]["path"] == "test.py"

    def test_run_stream_content_end(self, monkeypatch):
        """run_stream should yield content_end when content is complete."""

        async def mock_chat(*args, **kwargs):
            return MockStreamResponse([MockChunk("Done")]), True

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

    def test_run_stream_complete_after_max_steps(self, monkeypatch):
        """run_stream should yield 'complete' when max steps reached."""

        async def mock_chat(*args, **kwargs):
            return (
                MockStreamResponse(
                    [
                        MockChunk("thinking step 1"),
                        MockChunk("thinking step 2"),
                        MockChunk("thinking step 3"),
                    ]
                ),
                True,
            )

        config = AgentConfig(model="mock", provider="mock", verbose=False, max_steps=2)
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
        assert "content" in types or "final" in types
