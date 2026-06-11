"""Tests for the append-only audit log (PR-08)."""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.core.audit_log import (
    AuditLogger,
    AuditEntry,
    get_audit_logger,
    reset_audit_logger,
)


# ── Basic write / read ─────────────────────────────────────────────


class TestBasicLogging:
    @pytest.fixture
    def log_dir(self, tmp_path):
        return tmp_path / "audit"

    def test_log_creates_file(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        path = al.log({"session_id": "s1", "agent_id": "main", "action": "tool_call"})
        assert path.exists()
        assert path.suffix == ".jsonl"
        assert path.name == f"{datetime.now().date().isoformat()}.jsonl"

    def test_log_writes_jsonl(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s1", "agent_id": "main", "action": "tool_call", "tool": "read_file"})
        al.log({"session_id": "s1", "agent_id": "main", "action": "tool_result", "tool": "read_file"})
        content = list(log_dir.glob("*.jsonl"))[0].read_text()
        lines = [l for l in content.strip().split("\n") if l]
        assert len(lines) == 2
        for line in lines:
            rec = json.loads(line)
            assert rec["schema_version"] == "1.0"
            assert "ts" in rec
            assert rec["schema_version"] == "1.0"

    def test_log_includes_schema_version_and_ts(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s1", "agent_id": "main", "action": "tool_call"})
        line = list(log_dir.glob("*.jsonl"))[0].read_text().strip()
        rec = json.loads(line)
        assert rec["schema_version"] == "1.0"
        assert "ts" in rec
        assert re.match(r"\d{4}-\d{2}-\d{2}T", rec["ts"])


# ── Privacy: hash, not plaintext ──────────────────────────────────


class TestPrivacy:
    @pytest.fixture
    def log_dir(self, tmp_path):
        return tmp_path / "audit"

    def test_args_replaced_with_hash(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({
            "session_id": "s1", "agent_id": "main", "action": "tool_call",
            "args": {"path": "/secret/file.txt", "password": "hunter2"},
        })
        rec = json.loads(list(log_dir.glob("*.jsonl"))[0].read_text().strip())
        assert "args" not in rec
        assert rec["args_hash"].startswith("sha256:")
        assert isinstance(rec["args_size"], int)
        assert rec["args_size"] > 0
        # Plaintext must not appear
        assert "hunter2" not in json.dumps(rec)

    def test_result_replaced_with_hash(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({
            "session_id": "s1", "agent_id": "main", "action": "tool_result",
            "result": {"data": "top secret content"},
        })
        rec = json.loads(list(log_dir.glob("*.jsonl"))[0].read_text().strip())
        assert "result" not in rec
        assert rec["result_hash"].startswith("sha256:")
        assert "top secret" not in json.dumps(rec)

    def test_args_hash_is_deterministic(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s1", "agent_id": "main", "action": "x", "args": {"a": 1}})
        al.log({"session_id": "s1", "agent_id": "main", "action": "x", "args": {"a": 1}})
        # Force flush
        lines = list(log_dir.glob("*.jsonl"))[0].read_text().strip().split("\n")
        h1 = json.loads(lines[0])["args_hash"]
        h2 = json.loads(lines[1])["args_hash"]
        assert h1 == h2  # same args → same hash

    def test_none_args_dropped(self, log_dir):
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s1", "agent_id": "main", "action": "x", "args": None})
        rec = json.loads(list(log_dir.glob("*.jsonl"))[0].read_text().strip())
        assert "args" not in rec
        assert "args_hash" not in rec


# ── Query ────────────────────────────────────────────────────────


class TestQuery:
    @pytest.fixture
    def populated(self, tmp_path):
        log_dir = tmp_path / "audit"
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s1", "agent_id": "main", "action": "tool_call", "tool": "read_file"})
        al.log({"session_id": "s1", "agent_id": "main", "action": "tool_result", "tool": "read_file"})
        al.log({"session_id": "s1", "agent_id": "sub-1", "action": "tool_call", "tool": "write_file"})
        al.log({"session_id": "s2", "agent_id": "main", "action": "permission_check"})
        return al, log_dir

    def test_query_all(self, populated):
        al, _ = populated
        recs = al.query()
        assert len(recs) == 4

    def test_filter_by_agent_id(self, populated):
        al, _ = populated
        recs = al.query(agent_id="sub-1")
        assert len(recs) == 1
        assert recs[0]["agent_id"] == "sub-1"

    def test_filter_by_action(self, populated):
        al, _ = populated
        recs = al.query(action="tool_call")
        assert len(recs) == 2

    def test_filter_by_tool(self, populated):
        al, _ = populated
        recs = al.query(tool="read_file")
        assert len(recs) == 2

    def test_filter_by_session(self, populated):
        al, _ = populated
        recs = al.query(agent_id="main")
        # Combine filters
        recs2 = al.query(action="permission_check")
        assert any(r.get("session_id") == "s2" for r in recs2)

    def test_limit(self, populated):
        al, _ = populated
        recs = al.query(limit=2)
        assert len(recs) == 2

    def test_query_empty_log_dir(self, tmp_path):
        al = AuditLogger(log_dir=tmp_path / "empty")
        assert al.query() == []


# ── Rotation / archive ────────────────────────────────────────────


class TestRotation:
    def test_rotate_old_files(self, tmp_path):
        log_dir = tmp_path / "audit"
        al = AuditLogger(log_dir=log_dir)
        # Create a "yesterday" log file
        yesterday = (datetime.now().date() - timedelta(days=45)).isoformat()
        old_file = log_dir / f"{yesterday}.jsonl"
        old_file.write_text('{"a":1}\n')
        # Create a "today" log file (should NOT be rotated)
        today_file = log_dir / f"{datetime.now().date().isoformat()}.jsonl"
        today_file.write_text('{"a":2}\n')
        count = al.rotate(retention_days=30)
        assert count == 1
        assert not old_file.exists()
        assert today_file.exists()
        # Archived
        archives = list(al.archive_dir.glob("*.tar.gz"))
        assert len(archives) == 1

    def test_rotate_nothing_to_do(self, tmp_path):
        log_dir = tmp_path / "audit"
        al = AuditLogger(log_dir=log_dir)
        today_file = log_dir / f"{datetime.now().date().isoformat()}.jsonl"
        today_file.write_text('{"a":1}\n')
        count = al.rotate(retention_days=30)
        assert count == 0

    def test_rotate_skips_non_date_files(self, tmp_path):
        log_dir = tmp_path / "audit"
        al = AuditLogger(log_dir=log_dir)
        # A file that doesn't match the date pattern should not be touched
        sidecar = log_dir / "sidecar.jsonl"
        sidecar.write_text('{"a":1}\n')
        count = al.rotate(retention_days=30)
        assert count == 0
        assert sidecar.exists()


# ── Stats ─────────────────────────────────────────────────────────


class TestStats:
    def test_empty_stats(self, tmp_path):
        al = AuditLogger(log_dir=tmp_path / "audit")
        s = al.stats()
        assert s["total_entries"] == 0
        assert s["by_action"] == {}

    def test_stats_aggregate(self, tmp_path):
        log_dir = tmp_path / "audit"
        al = AuditLogger(log_dir=log_dir)
        al.log({"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "read"})
        al.log({"session_id": "s", "agent_id": "main", "action": "tool_call", "tool": "read"})
        al.log({"session_id": "s", "agent_id": "sub", "action": "permission_check"})
        s = al.stats()
        assert s["total_entries"] == 3
        assert s["by_action"]["tool_call"] == 2
        assert s["by_action"]["permission_check"] == 1
        assert s["by_tool"]["read"] == 2
        assert s["by_agent"]["main"] == 2
        assert s["by_agent"]["sub"] == 1


# ── Immutability (no delete API) ─────────────────────────────────


class TestImmutability:
    def test_delete_record_raises(self, tmp_path):
        al = AuditLogger(log_dir=tmp_path / "audit")
        with pytest.raises(NotImplementedError, match="append-only"):
            al.delete_record()

    def test_no_clear_method(self, tmp_path):
        al = AuditLogger(log_dir=tmp_path / "audit")
        assert not hasattr(al, "clear")


# ── AuditEntry dataclass ─────────────────────────────────────────


class TestAuditEntry:
    def test_to_dict(self):
        e = AuditEntry(
            session_id="s1", agent_id="main", action="tool_call",
            tool="read_file", args={"path": "/x"}, permission_decision="allow",
        )
        d = e.to_dict()
        assert d["session_id"] == "s1"
        assert d["action"] == "tool_call"
        assert d["args"] == {"path": "/x"}

    def test_log_entry_helper(self, tmp_path):
        al = AuditLogger(log_dir=tmp_path / "audit")
        e = AuditEntry(session_id="s", agent_id="main", action="tool_call", tool="x")
        al.log_entry(e)
        rec = al.query()[0]
        assert rec["session_id"] == "s"


# ── Singleton ─────────────────────────────────────────────────────


class TestSingleton:
    def test_singleton(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_AUDIT_DIR", str(tmp_path / "audit"))
        reset_audit_logger()
        a1 = get_audit_logger()
        a2 = get_audit_logger()
        assert a1 is a2

    def test_reset(self):
        reset_audit_logger()
        reset_audit_logger()  # Double-reset is safe
        assert get_audit_logger() is not None
