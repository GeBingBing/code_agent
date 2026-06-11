"""Tests for agent self-evolution (P2-1)."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.core.evolution import EvolutionEngine, FailurePattern
from agent.core.memory import MemoryManager


class TestEvolutionEngine:
    """Test skill extraction and failure recording."""

    def test_disabled_by_default(self, tmp_path):
        engine = EvolutionEngine(enabled=False, cache_dir=tmp_path)
        mem = MemoryManager()
        result = engine.analyze_run("test task", mem)
        assert result == {"skill_created": False, "failure_recorded": False, "actions": []}

    def test_success_skill_extraction(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        mem = MemoryManager()

        # Simulate a successful run: 2+ tool calls, no errors
        mem.add("assistant", "", tool_calls=json.dumps([{
            "id": "tc1",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path": "test.py", "content": "x=1"}'}
        }]))
        mem.add("tool", "File written: test.py", tool_call_id="tc1")
        mem.add("assistant", "", tool_calls=json.dumps([{
            "id": "tc2",
            "type": "function",
            "function": {"name": "execute_command", "arguments": '{"command": "pytest"}'}
        }]))
        mem.add("tool", "2 passed", tool_call_id="tc2")

        result = engine.analyze_run("write tests for auth module", mem)
        assert result["skill_created"] is True
        assert result["failure_recorded"] is False
        assert len(result["actions"]) == 1
        assert "Auto-skill extracted" in result["actions"][0]
        assert "auto_" in result["skill"]["name"]
        assert "testing" in result["skill"]["tags"]

    def test_failure_recording(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        mem = MemoryManager()

        # Simulate a failed run with error messages
        mem.add("assistant", "", tool_calls=json.dumps([{
            "id": "tc1",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path": "bad.py", "content": "x"}'}
        }]))
        mem.add("tool", "Error: Permission denied: bad.py", tool_call_id="tc1")

        result = engine.analyze_run("fix auth bug", mem)
        assert result["skill_created"] is False
        assert result["failure_recorded"] is True
        assert len(result["actions"]) == 1
        assert "Failure pattern recorded" in result["actions"][0]
        assert "Permission denied" in result["failure"]["error_signature"]

    def test_failure_deduplication(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)

        mem1 = MemoryManager()
        mem1.add("tool", "Error: ModuleNotFoundError: No module named 'foo'", tool_call_id="tc1")

        mem2 = MemoryManager()
        mem2.add("tool", "Error: ModuleNotFoundError: No module named 'foo'", tool_call_id="tc2")

        r1 = engine.analyze_run("import foo", mem1)
        r2 = engine.analyze_run("import foo again", mem2)

        assert r1["failure"]["count"] == 1
        assert r2["failure"]["count"] == 2
        assert r2["failure"]["dedup"] is True

    def test_get_failure_context(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        mem = MemoryManager()
        mem.add("tool", "Error: SyntaxError: invalid syntax", tool_call_id="tc1")
        engine.analyze_run("parse yaml", mem)

        ctx = engine.get_failure_context("parse yaml config")
        assert "Past failures" in ctx
        assert "SyntaxError" in ctx
        assert "occurred 1x" in ctx

    def test_no_relevant_failures(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        mem = MemoryManager()
        mem.add("tool", "Error: RuntimeError: timeout", tool_call_id="tc1")
        engine.analyze_run("network request", mem)

        ctx = engine.get_failure_context("completely unrelated task")
        assert ctx == ""

    def test_skill_type_detection(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        mem = MemoryManager()
        mem.add("assistant", "", tool_calls=json.dumps([{
            "id": "tc1",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{}"}
        }]))
        mem.add("tool", "ok", tool_call_id="tc1")
        mem.add("assistant", "", tool_calls=json.dumps([{
            "id": "tc2",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{}"}
        }]))
        mem.add("tool", "ok", tool_call_id="tc2")

        test_cases = [
            ("create a docker image for the app", "docker"),
            ("write a quick sort algorithm", "algorithm"),
            ("add rest api endpoints", "api"),
            ("run pytest on the codebase", "testing"),
            ("commit and push changes", "git"),
        ]

        for task, expected_tag in test_cases:
            result = engine.analyze_run(task, mem)
            assert result["skill_created"] is True
            assert expected_tag in result["skill"]["tags"]

    def test_load_failure_patterns(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        # Pre-populate failure log
        engine._append_jsonl(engine.failure_log, {
            "task_type": "test",
            "error_signature": "ValueError: bad",
            "context": "failed during write_file",
            "resolution": "",
            "count": 3,
        })

        patterns = engine._load_failure_patterns()
        assert len(patterns) == 1
        assert patterns[0].error_signature == "ValueError: bad"
        assert patterns[0].count == 3

    def test_save_and_load_failure_patterns(self, tmp_path):
        engine = EvolutionEngine(enabled=True, cache_dir=tmp_path)
        patterns = [
            FailurePattern(task_type="t1", error_signature="e1", context="c1", resolution="r1", count=1),
            FailurePattern(task_type="t2", error_signature="e2", context="c2", resolution="", count=2),
        ]
        engine._save_failure_patterns(patterns)
        loaded = engine._load_failure_patterns()
        assert len(loaded) == 2
        assert loaded[0].error_signature == "e1"
        assert loaded[1].count == 2


class TestEvolutionEngineIntegration:
    """Integration with AgentEngine."""

    def test_config_auto_evolve(self, monkeypatch):
        from agent.core.engine import AgentConfig, AgentEngine
        config = AgentConfig(model="mock", provider="mock", auto_evolve=True)
        agent = AgentEngine(config)
        assert agent.evolution.enabled is True
        assert agent.config.auto_evolve is True

    def test_evolution_disabled_by_default(self, monkeypatch):
        from agent.core.engine import AgentConfig, AgentEngine
        config = AgentConfig(model="mock", provider="mock")
        agent = AgentEngine(config)
        assert agent.evolution.enabled is False
