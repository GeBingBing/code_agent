"""Unit tests for the progress anchor (PR-13)."""

import re

import pytest

from agent.core.progress_anchor import (
    ProgressAnchor,
    ProgressRecord,
    load_progress,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _sample_record() -> ProgressRecord:
    return ProgressRecord(
        current_task="Implement auth API",
        current_step="3/8 (writing login endpoint)",
        next_step="4/8 (write auth middleware test)",
        op_hash="sha256:abc123",
        known_issues=["rate limiting 未实现", "no retry on transient errors"],
        updated_at="2026-06-06T10:23:45",
    )


# ── TestProgressRecord ─────────────────────────────────────────────


class TestProgressRecord:
    def test_defaults(self):
        r = ProgressRecord()
        assert r.current_task == ""
        assert r.current_step == ""
        assert r.next_step == ""
        assert r.op_hash == ""
        assert r.known_issues == []
        assert r.updated_at == ""
        assert r.extra == {}

    def test_is_empty(self):
        assert ProgressRecord().is_empty()
        r = ProgressRecord()
        r.current_task = "x"
        assert not r.is_empty()

    def test_to_prompt(self):
        r = _sample_record()
        text = r.to_prompt()
        assert "Implement auth API" in text
        assert "3/8" in text
        assert "4/8" in text
        assert "rate limiting" in text

    def test_to_prompt_no_issues(self):
        r = ProgressRecord(current_task="x", current_step="1/2", next_step="2/2")
        text = r.to_prompt()
        assert "none" in text
        assert "rate limiting" not in text


# ── TestPathResolution ────────────────────────────────────────────


class TestPathResolution:
    def test_default_workspace(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        anchor = ProgressAnchor()
        assert anchor.path == tmp_path / ".claude-progress.txt"

    def test_explicit_workspace(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        assert anchor.path == tmp_path / ".claude-progress.txt"

    def test_file_name_constant(self):
        assert ProgressAnchor.FILE_NAME == ".claude-progress.txt"


# ── TestExists ─────────────────────────────────────────────────────


class TestExists:
    def test_does_not_exist(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        assert not anchor.exists()

    def test_does_exist(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="x"))
        assert anchor.exists()


# ── TestReadMissingFile ────────────────────────────────────────────


class TestReadMissingFile:
    def test_returns_none(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        assert anchor.read() is None


# ── TestWriteReadRoundTrip ────────────────────────────────────────


class TestWriteReadRoundTrip:
    def test_basic(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = _sample_record()
        anchor.write(rec)
        loaded = anchor.read()
        assert loaded is not None
        assert loaded.current_task == rec.current_task
        assert loaded.current_step == rec.current_step
        assert loaded.next_step == rec.next_step
        assert loaded.op_hash == rec.op_hash
        assert loaded.known_issues == rec.known_issues
        assert loaded.updated_at == rec.updated_at

    def test_sets_updated_at_if_missing(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(current_task="x")
        rec.updated_at = ""
        anchor.write(rec)
        loaded = anchor.read()
        assert loaded.updated_at != ""

    def test_preserves_existing_updated_at(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(current_task="x", updated_at="2020-01-01T00:00:00")
        anchor.write(rec)
        loaded = anchor.read()
        assert loaded.updated_at == "2020-01-01T00:00:00"


# ── TestParsingVariants ────────────────────────────────────────────


class TestParsingVariants:
    def test_minimal(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]: minimal\n[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.current_task == "minimal"
        assert r.updated_at == "now"

    def test_no_known_issues(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]: x\n[known_issues]:\n[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.known_issues == []

    def test_inline_known_issue(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]: x\n[known_issues]: - first issue\n[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.known_issues == ["first issue"]

    def test_multiline_known_issues(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]: x\n"
            "[known_issues]:\n"
            "  - issue one\n"
            "  - issue two\n"
            "  - issue three\n"
            "[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.known_issues == ["issue one", "issue two", "issue three"]

    def test_unknown_key_kept_in_extra(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]: x\n" "[custom_key]: custom_value\n" "[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.extra.get("custom_key") == "custom_value"

    def test_blank_lines_ignored(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "\n[current_task]: x\n\n[updated_at]: now\n\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.current_task == "x"

    def test_garbage_lines_ignored(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "this is not a key\n[malformed\n[current_task]: ok\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.current_task == "ok"

    def test_empty_value(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text(
            "[current_task]:\n[updated_at]: now\n",
            encoding="utf-8",
        )
        r = anchor.read()
        assert r.current_task == ""


# ── TestAtomicWrite ───────────────────────────────────────────────


class TestAtomicWrite:
    def test_no_temp_files_left(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="x"))
        # Look for any .tmp files
        temps = list(tmp_path.glob(".claude-progress.*.tmp"))
        assert temps == []

    def test_write_overwrites(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="first"))
        anchor.write(ProgressRecord(current_task="second"))
        r = anchor.read()
        assert r.current_task == "second"

    def test_writes_creates_directory(self, tmp_path):
        nested = tmp_path / "a" / "b"
        anchor = ProgressAnchor(workspace=nested)
        anchor.write(ProgressRecord(current_task="x"))
        assert anchor.path.exists()


# ── TestClear ──────────────────────────────────────────────────────


class TestClear:
    def test_clear_existing(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="x"))
        assert anchor.exists()
        anchor.clear()
        assert not anchor.exists()

    def test_clear_missing(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        # Should not raise
        anchor.clear()
        assert not anchor.exists()


# ── TestComputeHash ───────────────────────────────────────────────


class TestComputeHash:
    def test_format(self):
        h = ProgressAnchor.compute_hash("sha256:abc", "write_file:src/x.py")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 32  # prefix + 32 hex chars

    def test_deterministic(self):
        h1 = ProgressAnchor.compute_hash("sha256:abc", "op1")
        h2 = ProgressAnchor.compute_hash("sha256:abc", "op1")
        assert h1 == h2

    def test_different_inputs_yield_different_hashes(self):
        h1 = ProgressAnchor.compute_hash("sha256:abc", "op1")
        h2 = ProgressAnchor.compute_hash("sha256:abc", "op2")
        assert h1 != h2

    def test_different_prev_yields_different_hashes(self):
        h1 = ProgressAnchor.compute_hash("sha256:abc", "op1")
        h2 = ProgressAnchor.compute_hash("sha256:xyz", "op1")
        assert h1 != h2

    def test_empty_prev(self):
        h = ProgressAnchor.compute_hash("", "op1")
        assert h.startswith("sha256:")

    def test_chain_property(self):
        # Computing h3 = H(h2, op3) after h2 = H(h1, op2) after h1 = H(h0, op1)
        # should equal a single chained call.
        h1 = ProgressAnchor.compute_hash("", "op1")
        h2 = ProgressAnchor.compute_hash(h1, "op2")
        h3 = ProgressAnchor.compute_hash(h2, "op3")
        h_chain = ProgressAnchor.compute_hash(
            ProgressAnchor.compute_hash(ProgressAnchor.compute_hash("", "op1"), "op2"),
            "op3",
        )
        assert h3 == h_chain


# ── TestVerifyChain ───────────────────────────────────────────────


class TestVerifyChain:
    def test_no_file_is_ok(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        assert anchor.verify_chain() is True

    def test_existing_file_is_ok(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(_sample_record())
        assert anchor.verify_chain() is True


# ── TestRender ─────────────────────────────────────────────────────


class TestRender:
    def test_no_file(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        assert "(no progress file)" in anchor.render()

    def test_with_file(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(_sample_record())
        text = anchor.render()
        assert "Implement auth API" in text


# ── TestLoadProgress ──────────────────────────────────────────────


class TestLoadProgress:
    def test_no_file(self, tmp_path):
        assert load_progress(workspace=tmp_path) is None

    def test_with_file(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(_sample_record())
        r = load_progress(workspace=tmp_path)
        assert r is not None
        assert r.current_task == "Implement auth API"


# ── TestExtraFieldsPreservedOnWrite ───────────────────────────────


class TestExtraFieldsPreservedOnWrite:
    def test_extra_written_to_file(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(current_task="x")
        rec.extra["current_ac"] = "P0-1"
        rec.extra["session_id"] = "abc"
        anchor.write(rec)
        loaded = anchor.read()
        assert loaded.extra.get("current_ac") == "P0-1"
        assert loaded.extra.get("session_id") == "abc"


# ── TestHashFormatPrefixConsistency ──────────────────────────────


class TestHashFormatPrefixConsistency:
    def test_matches_audit_log(self):
        # Both PR-08 audit and PR-13 use the same sha256:hex[:32] format
        h = ProgressAnchor.compute_hash("prev", "op")
        assert h.startswith("sha256:")
        assert re.match(r"^sha256:[a-f0-9]{32}$", h)


# ── TestIsEmptyOnParsed ────────────────────────────────────────────


class TestIsEmptyOnParsed:
    def test_empty_file_parses_to_empty(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.path.write_text("\n\n", encoding="utf-8")
        r = anchor.read()
        # An "empty" file still produces a record, but with no values
        # except the default updated_at set by write(); read() of an
        # un-writen file should not set updated_at, so is_empty() = True
        assert r is not None
        assert r.is_empty()


# ── TestUnicodeAndSpecialChars ────────────────────────────────────


class TestUnicodeAndSpecialChars:
    def test_unicode_task_name(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(
            current_task="实现带鉴权的 API (中文 + emoji 🚀)",
            current_step="1/5",
        )
        anchor.write(rec)
        loaded = anchor.read()
        assert loaded.current_task == "实现带鉴权的 API (中文 + emoji 🚀)"

    def test_unicode_in_known_issues(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(
            current_task="x",
            known_issues=["rate limiting 未实现", "权限校验错误 ❌"],
        )
        anchor.write(rec)
        loaded = anchor.read()
        assert "rate limiting 未实现" in loaded.known_issues
        assert "权限校验错误 ❌" in loaded.known_issues

    def test_chain_hash_with_unicode_op(self, tmp_path):
        h = ProgressAnchor.compute_hash("prev", 'write_file:{"path": "测试.py"}')
        # Should still produce valid format
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 32

    def test_chain_hash_with_emoji_op(self, tmp_path):
        h1 = ProgressAnchor.compute_hash("p", "op🚀")
        h2 = ProgressAnchor.compute_hash("p", "op🎉")
        # Different emojis → different hash
        assert h1 != h2

    def test_known_issue_with_special_chars(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(
            current_task="x",
            known_issues=['tool: error: "unclosed quote" & <html>'],
        )
        anchor.write(rec)
        loaded = anchor.read()
        assert 'tool: error: "unclosed quote" & <html>' in loaded.known_issues


# ── TestLongContent ───────────────────────────────────────────────


class TestLongContent:
    def test_long_known_issues_list(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(
            current_task="x",
            known_issues=[f"issue {i}: error {i}" for i in range(100)],
        )
        anchor.write(rec)
        loaded = anchor.read()
        assert len(loaded.known_issues) == 100
        assert loaded.known_issues[42] == "issue 42: error 42"

    def test_very_long_task_name(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(current_task="x" * 1000, current_step="1/1")
        anchor.write(rec)
        loaded = anchor.read()
        assert len(loaded.current_task) == 1000

    def test_very_long_op_hash_chain(self, tmp_path):
        """Chaining 50 hashes should keep them all valid sha256:hex[:32]."""
        prev = ""
        for i in range(50):
            prev = ProgressAnchor.compute_hash(prev, f"op_{i}")
            assert prev.startswith("sha256:")
            assert len(prev) == 7 + 32
        # Final hash should be deterministic
        prev2 = ""
        for i in range(50):
            prev2 = ProgressAnchor.compute_hash(prev2, f"op_{i}")
        assert prev == prev2


# ── TestRenderEdgeCases ──────────────────────────────────────────


class TestRenderEdgeCases:
    def test_render_unset(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        # Don't write anything
        assert anchor.render() == "(no progress file)"

    def test_render_known_issues_joined(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(
            current_task="x",
            known_issues=["issue1", "issue2"],
        )
        anchor.write(rec)
        text = anchor.render()
        assert "issue1, issue2" in text


# ── TestWriteReturnValue ─────────────────────────────────────────


class TestWriteReturnValue:
    def test_write_returns_path(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        rec = ProgressRecord(current_task="x")
        result_path = anchor.write(rec)
        assert result_path == anchor.path
        assert result_path.exists()


# ── TestProgressRecordDefaults ───────────────────────────────────


class TestProgressRecordDefaults:
    def test_all_fields_default_empty(self):
        rec = ProgressRecord()
        assert rec.current_task == ""
        assert rec.current_step == ""
        assert rec.next_step == ""
        assert rec.op_hash == ""
        assert rec.known_issues == []
        assert rec.updated_at == ""
        assert rec.extra == {}
        assert rec.is_empty()

    def test_to_prompt_with_all_unset(self):
        rec = ProgressRecord()
        text = rec.to_prompt()
        assert "current_task: (unset)" in text
        assert "current_step: (unset)" in text
        # next_step has alignment padding (4 spaces after the colon)
        assert "next_step: " in text and "(unset)" in text
        assert "known_issues: none" in text

    def test_is_empty_with_only_known_issues(self):
        rec = ProgressRecord(known_issues=["x"])
        assert not rec.is_empty()

    def test_is_empty_with_only_extra(self):
        rec = ProgressRecord(extra={"k": "v"})
        assert not rec.is_empty()

    def test_to_prompt_known_issues_inline(self):
        rec = ProgressRecord(known_issues=["a", "b"])
        text = rec.to_prompt()
        assert "a, b" in text


# ── TestFileAtomicityStress ──────────────────────────────────────


class TestFileAtomicityStress:
    def test_50_writes_no_temp_files(self, tmp_path):
        """50 sequential writes should not leave .tmp files behind."""
        anchor = ProgressAnchor(workspace=tmp_path)
        for i in range(50):
            anchor.write(
                ProgressRecord(
                    current_task=f"task {i}",
                    current_step=f"{i}/50",
                    op_hash=f"sha256:hash{i:032d}"[:39],
                )
            )
        tmp_files = list(anchor.path.parent.glob(".claude-progress.*.tmp"))
        assert tmp_files == []
        # Final write has task "task 49"
        rec = anchor.read()
        assert rec.current_task == "task 49"

    def test_overwrite_preserves_latest_data(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="first", op_hash="sha256:first"))
        anchor.write(ProgressRecord(current_task="second", op_hash="sha256:second"))
        loaded = anchor.read()
        assert loaded.current_task == "second"
        assert loaded.op_hash == "sha256:second"


# ── TestLoadProgressConvenience ──────────────────────────────────


class TestLoadProgressConvenience:
    def test_load_progress_no_file(self, tmp_path):
        assert load_progress(tmp_path) is None

    def test_load_progress_with_file(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="x"))
        rec = load_progress(tmp_path)
        assert rec is not None
        assert rec.current_task == "x"

    def test_load_progress_string_path(self, tmp_path):
        anchor = ProgressAnchor(workspace=tmp_path)
        anchor.write(ProgressRecord(current_task="x"))
        # String path should also work (Path auto-converted internally)
        rec = load_progress(str(tmp_path))
        assert rec is not None


# ── TestWorkspaceNotDir ──────────────────────────────────────────


class TestWorkspaceNotDir:
    def test_workspace_is_file_raises(self, tmp_path):
        """If workspace path exists but is a file, mkdir(parents=True, exist_ok=True)
        will fail with FileExistsError when we try to write."""
        not_a_dir = tmp_path / "iamafile.txt"
        not_a_dir.write_text("not a dir")
        anchor = ProgressAnchor(workspace=not_a_dir)
        # The file path would be `<not_a_dir>/.claude-progress.txt`
        # mkdir will fail when we try to write
        with pytest.raises((FileExistsError, OSError)):
            anchor.write(ProgressRecord(current_task="x"))


# ── TestChainHashProperties ──────────────────────────────────────


class TestChainHashProperties:
    def test_avalanche_single_bit_change(self):
        """Changing one character in the op should completely change the
        hash (avalanche property)."""
        h1 = ProgressAnchor.compute_hash("prev", "write_file:a.py")
        h2 = ProgressAnchor.compute_hash("prev", "write_file:b.py")
        assert h1 != h2
        # And the difference should be substantial (>50% of chars differ)
        hex1 = h1[7:]
        hex2 = h2[7:]
        diff = sum(c1 != c2 for c1, c2 in zip(hex1, hex2, strict=False))
        assert diff > 16  # More than half

    def test_empty_op_still_hashes(self):
        h = ProgressAnchor.compute_hash("prev", "")
        assert h.startswith("sha256:")
        # Different from prev alone
        h2 = ProgressAnchor.compute_hash("prev", "x")
        assert h != h2

    def test_chain_avalanche_at_each_step(self):
        """Verify that each step in a long chain has a different hash
        (no collisions)."""
        prev = ""
        hashes = set()
        for i in range(100):
            prev = ProgressAnchor.compute_hash(prev, f"op_{i}")
            hashes.add(prev)
        # 100 unique hashes
        assert len(hashes) == 100

    def test_hash_format_exactly_39_chars(self):
        h = ProgressAnchor.compute_hash("p", "o")
        # "sha256:" (7 chars) + 32 hex chars
        assert len(h) == 39
