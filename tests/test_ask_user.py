"""Tests for the ask_user tool — structured multi-choice questioning."""

import pytest

from agent.tools.ask_user import AskUserTool
from agent.tools.base import registry


class TestAskUserRegistration:
    def test_registered(self):
        import agent.core.engine  # noqa: F401 — triggers registration

        assert "ask_user" in [t.name for t in registry.list()]

    def test_low_risk(self):
        from agent.core.permissions import RiskLevel, assess_risk

        assert assess_risk("ask_user", {"question": "x?", "options": []}) == RiskLevel.LOW


class TestAskUserSchema:
    def test_schema_has_required_fields(self):
        tool = AskUserTool()
        schema = tool.schema["function"]["parameters"]
        props = schema["properties"]
        assert "question" in props
        assert "options" in props
        assert "multi_select" in props
        assert schema["required"] == ["question", "options"]
        # 2-4 options enforced.
        assert props["options"]["minItems"] == 2
        assert props["options"]["maxItems"] == 4

    def test_render_call_shows_question(self):
        tool = AskUserTool()
        assert tool.render_call({"question": "Which framework?"}) == "Which framework?"

    def test_render_result_shows_choice(self):
        tool = AskUserTool()

        class _R:
            success = True
            content = "User chose: Option A"
            error = None
            metadata = None

        assert tool.render_result(_R()) == "User chose: Option A"


class TestAskUserExecute:
    @pytest.mark.asyncio
    async def test_single_select_returns_chosen_label(self, monkeypatch):
        tool = AskUserTool()
        # Simulate the user typing "1".
        monkeypatch.setattr("sys.stdin", _FakeStdin("1\n"))
        result = await tool.execute(
            question="Which approach?",
            options=[
                {"label": "Option A", "description": "faster"},
                {"label": "Option B", "description": "safer"},
            ],
        )
        assert result.success
        assert "Option A" in result.content
        assert result.metadata["choices"] == ["Option A"]

    @pytest.mark.asyncio
    async def test_multi_select_returns_all_chosen(self, monkeypatch):
        tool = AskUserTool()
        monkeypatch.setattr("sys.stdin", _FakeStdin("1,2\n"))
        result = await tool.execute(
            question="Pick features",
            options=[{"label": "A"}, {"label": "B"}, {"label": "C"}],
            multi_select=True,
        )
        assert result.success
        assert result.metadata["choices"] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_cancel_returns_failure(self, monkeypatch):
        tool = AskUserTool()
        monkeypatch.setattr("sys.stdin", _FakeStdin("0\n"))
        result = await tool.execute(question="Q?", options=[{"label": "A"}, {"label": "B"}])
        assert not result.success
        assert "cancel" in result.error.lower()

    @pytest.mark.asyncio
    async def test_too_few_options_fails(self):
        tool = AskUserTool()
        result = await tool.execute(question="Q?", options=[{"label": "A"}])
        assert not result.success
        assert "at least 2" in result.error

    @pytest.mark.asyncio
    async def test_invalid_input_fails(self, monkeypatch):
        tool = AskUserTool()
        monkeypatch.setattr("sys.stdin", _FakeStdin("xyz\n"))
        result = await tool.execute(question="Q?", options=[{"label": "A"}, {"label": "B"}])
        assert not result.success


class _FakeStdin:
    """Minimal stdin stub supporting readline()."""

    def __init__(self, text: str):
        self._text = text

    def readline(self):
        return self._text
