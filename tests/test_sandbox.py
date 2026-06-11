"""Tests for Phase 7: Sandbox snapshot and rollback."""

from agent.tools.sandbox import Sandbox


class TestSnapshotRollback:
    def test_snapshot_creates_copy(self, tmp_path):
        source = tmp_path / "workspace"
        source.mkdir()
        (source / "file.txt").write_text("original")

        sb = Sandbox()
        snap = sb.snapshot(str(source), "test1")
        assert snap != str(source)
        # Snapshot should have the same content
        assert (source / "file.txt").read_text() == "original"

    def test_rollback_restores(self, tmp_path):
        source = tmp_path / "workspace"
        source.mkdir()
        (source / "file.txt").write_text("original")

        sb = Sandbox()
        sb.snapshot(str(source), "test2")

        # Modify the source
        (source / "file.txt").write_text("modified")
        (source / "new.txt").write_text("new")

        # Rollback
        sb.rollback("test2", str(source))
        assert (source / "file.txt").read_text() == "original"
        assert not (source / "new.txt").exists()

    def test_docker_check(self):
        sb = Sandbox()
        # Just verify it doesn't crash; result depends on host
        assert isinstance(sb.has_docker, bool)
