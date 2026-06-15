"""Contract tests for the mock LLM provider.

These tests guarantee the property hay reported was missing:
    "OPENAI_API_KEY=mock should not hit the network"

They run against the real `agent/llm/client.py` mock backend (not against
test-side stubs), so any regression in the short-circuit logic fails here.
"""

import asyncio

import pytest

from agent.llm.client import LLMClient, _MockLLMBackend


class TestMockLLMShortCircuit:
    """Mock provider must NEVER touch the network."""

    def test_construction_no_api_key_required(self):
        """provider='mock' should construct with no api_key in env."""
        client = LLMClient(provider="mock", model="mock")
        assert client.client is None, "mock provider should have no openai client"
        assert isinstance(client._mock, _MockLLMBackend)

    @pytest.mark.asyncio
    async def test_default_response_is_OK(self, mock_llm):
        """Without scripting, chat() returns canned 'OK'."""
        result = await mock_llm.chat([])
        assert result == "OK"

    @pytest.mark.asyncio
    async def test_queued_string_response(self, mock_llm):
        mock_llm.queue_response("computed-answer")
        result = await mock_llm.chat([])
        assert result == "computed-answer"

    @pytest.mark.asyncio
    async def test_queued_responses_are_FIFO(self, mock_llm):
        mock_llm.queue_response("first")
        mock_llm.queue_response("second")
        assert await mock_llm.chat([]) == "first"
        assert await mock_llm.chat([]) == "second"
        # Exhausted → fallback to "OK"
        assert await mock_llm.chat([]) == "OK"

    @pytest.mark.asyncio
    async def test_call_log_captures_messages(self, mock_llm):
        from agent.llm.client import Message

        m = Message(role="user", content="hi")
        await mock_llm.chat([m])
        assert len(mock_llm.mock_call_log) == 1
        assert mock_llm.mock_call_log[0]["messages"][0].content == "hi"
        assert mock_llm.mock_call_log[0]["stream"] is False

    @pytest.mark.asyncio
    async def test_tool_call_response(self, mock_llm, mock_message_factory):
        msg = mock_message_factory.with_tool_call(
            call_id="call_1",
            name="read_file",
            arguments={"path": "/tmp/foo"},
        )
        mock_llm.queue_response(msg)
        result = await mock_llm.chat([])
        assert result is msg, "tool_calls message should be returned as-is"
        assert result.tool_calls[0].function.name == "read_file"

    @pytest.mark.asyncio
    async def test_stream_default_yields_OK(self, mock_llm):
        stream_iter, is_stream = await mock_llm.chat([], stream=True)
        assert is_stream is True
        chunks = list(stream_iter)
        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content == "OK"
        assert chunks[0].choices[0].finish_reason == "stop"

    def test_queue_on_real_provider_raises(self, monkeypatch):
        """queue_response on a non-mock provider must raise."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-real")
        try:
            real = LLMClient(provider="openai", model="gpt-4o-mini")
        except Exception:
            pytest.skip("could not construct openai client in this env")
        with pytest.raises(RuntimeError, match="provider='mock'"):
            real.queue_response("x")

    @pytest.mark.asyncio
    async def test_reset_mock_clears_state(self, mock_llm):
        mock_llm.queue_response("a")
        await mock_llm.chat([])
        assert len(mock_llm.mock_call_log) == 1
        mock_llm.reset_mock()
        assert mock_llm.mock_call_log == []
        # Queue is also cleared, so next call returns canned OK
        assert await mock_llm.chat([]) == "OK"

    def test_no_network_socket_opened(self):
        """Hard guarantee: constructing + using mock client opens NO inet sockets.

        We wrap socket.socket so AF_INET / AF_INET6 attempts are flagged.
        AF_UNIX is allowed (used by logging, asyncio self-pipes, etc).
        """
        import socket

        original = socket.socket
        inet_opens: list = []

        def tripwire(family=socket.AF_INET, *args, **kwargs):
            if family in (socket.AF_INET, socket.AF_INET6):
                inet_opens.append((family, args, kwargs))
            return original(family, *args, **kwargs)

        socket.socket = tripwire
        try:
            client = LLMClient(provider="mock", model="mock")
            client.queue_response("hello")
            asyncio.run(client.chat([]))
            client.queue_stream_chunks(["hi"])
            asyncio.run(client.chat([], stream=True))
        finally:
            socket.socket = original

        assert inet_opens == [], (
            f"mock LLM opened {len(inet_opens)} INET socket(s) — short-circuit broken"
        )
