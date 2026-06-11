"""Tests for streaming LLM responses."""

import asyncio

import pytest

from agent.llm.client import LLMClient, Message


class MockStreamResponse:
    """Simulate OpenAI streaming response."""
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


class TestStreamingChat:
    """Test streaming chat implementation."""

    def test_streaming_returns_generator(self, monkeypatch):
        """When stream=True, chat() should return a generator."""
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")

        c = LLMClient(model="MiniMax-M2.7", provider="minimax", api_key="test-key")
        c.client = type('Client', (), {
            'chat': type('Chat', (), {
                'completions': type('Completions', (), {
                    'create': lambda *args, **kw: MockStreamResponse([
                        type('Chunk', (), {
                            'choices': [type('Choice', (), {'delta': type('Delta', (), {'content': 'Hello '})()})()]
                        })(),
                        type('Chunk', (), {
                            'choices': [type('Choice', (), {'delta': type('Delta', (), {'content': 'world'})()})()]
                        })(),
                    ])
                })()
            })()
        })()

        async def run():
            return await c.chat(
                messages=[Message(role="user", content="hi")],
                stream=True
            )

        result = asyncio.run(run())
        # Should be a generator-like object
        assert hasattr(result, '__iter__')

    def test_non_streaming_returns_content(self, monkeypatch):
        """When stream=False, chat() should return string."""
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

        c = LLMClient(model="MiniMax-M2.7", provider="minimax", api_key="test-key")
        c.client = type('Client', (), {
            'chat': type('Chat', (), {
                'completions': type('Completions', (), {
                    'create': lambda *args, **kw: type('Response', (), {
                        'choices': [type('Choice', (), {
                            'message': type('Msg', (), {'content': 'Hello world'})
                        })]
                    })()
                })()
            })()
        })()

        async def run():
            return await c.chat(
                messages=[Message(role="user", content="hi")],
                stream=False
            )

        result = asyncio.run(run())
        assert result == "Hello world"