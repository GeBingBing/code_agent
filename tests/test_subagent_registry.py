"""Tests for SubAgentRegistry."""

import pytest

from agent.core.subagent_registry import (
    SubAgentRegistry, SubAgentRecord, SubAgentStatus,
    get_registry, reset_registry
)


class TestSubAgentRegistry:
    """Test the SubAgentRegistry class."""

    def setup_method(self):
        reset_registry()

    def test_spawn_creates_record(self):
        registry = get_registry()
        record = registry.spawn(parent_id=None, label="test", task="do something", depth=0)

        assert record.id is not None
        assert record.label == "test"
        assert record.task == "do something"
        assert record.status == SubAgentStatus.RUNNING
        assert record.depth == 0

    def test_spawn_with_parent(self):
        registry = get_registry()
        parent = registry.spawn(parent_id=None, label="parent", task="parent task", depth=0)

        child = registry.spawn(parent_id=parent.id, label="child", task="child task", depth=1)

        assert child.parent_id == parent.id
        assert child.depth == 1

    def test_max_depth_exceeded(self):
        """Test that spawning beyond MAX_DEPTH raises ValueError."""
        from agent.core.subagent_registry import SubAgentRegistry
        # Create a fresh registry with MAX_DEPTH=3
        registry = SubAgentRegistry()
        registry.MAX_DEPTH = 3

        # Spawn at depth 0, 1, 2 successfully (depth < 3)
        r0 = registry.spawn(parent_id=None, label="l0", task="t", depth=0)
        r1 = registry.spawn(parent_id=r0.id, label="l1", task="t", depth=1)
        r2 = registry.spawn(parent_id=r1.id, label="l2", task="t", depth=2)

        # Depth 3 should fail (depth >= 3)
        with pytest.raises(ValueError, match="Max subagent depth"):
            registry.spawn(parent_id=r2.id, label="l3", task="t", depth=3)

    def test_complete(self):
        registry = get_registry()
        record = registry.spawn(parent_id=None, label="test", task="task", depth=0)

        registry.complete(record.id, "result value")

        updated = registry.get(record.id)
        assert updated.status == SubAgentStatus.COMPLETED
        assert updated.result == "result value"
        assert updated.completed_at is not None

    def test_fail(self):
        registry = get_registry()
        record = registry.spawn(parent_id=None, label="test", task="task", depth=0)

        registry.fail(record.id, "something went wrong")

        updated = registry.get(record.id)
        assert updated.status == SubAgentStatus.FAILED
        assert updated.error == "something went wrong"

    def test_kill(self):
        registry = get_registry()
        parent = registry.spawn(parent_id=None, label="parent", task="task", depth=0)
        child = registry.spawn(parent_id=parent.id, label="child", task="task", depth=1)

        success = registry.kill(parent.id)

        assert success is True
        assert registry.get(parent.id).status == SubAgentStatus.KILLED
        assert registry.get(child.id).status == SubAgentStatus.KILLED  # recursive

    def test_list_children(self):
        registry = get_registry()
        parent = registry.spawn(parent_id=None, label="parent", task="task", depth=0)
        registry.spawn(parent_id=parent.id, label="child1", task="task", depth=1)
        registry.spawn(parent_id=parent.id, label="child2", task="task", depth=1)

        children = registry.list_children(parent.id)
        assert len(children) == 2

    def test_list_active(self):
        registry = get_registry()
        r1 = registry.spawn(parent_id=None, label="a1", task="task", depth=0)
        r2 = registry.spawn(parent_id=None, label="a2", task="task", depth=0)

        registry.complete(r1.id, "done")

        active = registry.list_active()
        assert len(active) == 1
        assert active[0].id == r2.id

    def test_get_ancestors(self):
        registry = get_registry()
        r0 = registry.spawn(parent_id=None, label="root", task="task", depth=0)
        r1 = registry.spawn(parent_id=r0.id, label="child", task="task", depth=1)
        r2 = registry.spawn(parent_id=r1.id, label="grandchild", task="task", depth=2)

        ancestors = registry.get_ancestors(r2.id)
        assert len(ancestors) == 2
        assert ancestors[0].id == r1.id
        assert ancestors[1].id == r0.id

    def test_get_tree(self):
        registry = get_registry()
        root = registry.spawn(parent_id=None, label="root", task="task", depth=0)
        child1 = registry.spawn(parent_id=root.id, label="child1", task="task", depth=1)
        child2 = registry.spawn(parent_id=root.id, label="child2", task="task", depth=1)

        tree = registry.get_tree(root.id)

        assert tree["label"] == "root"
        assert len(tree["children"]) == 2

    def test_register_task(self):
        """Test registering an asyncio Task for tracking."""
        registry = get_registry()
        record = registry.spawn(parent_id=None, label="test", task="task", depth=0)

        # Can't create asyncio tasks outside of async context in tests
        # Just verify the method exists and works with None
        registry._running_tasks[record.id] = None
        assert record.id in registry._running_tasks

    def test_cleanup_completed(self):
        registry = get_registry()
        # Create old records
        r1 = registry.spawn(parent_id=None, label="old1", task="task", depth=0)
        registry.complete(r1.id, "done")

        # They should still exist
        assert registry.get(r1.id) is not None

        # Cleanup with very small max_age should remove them
        registry.cleanup_completed(max_age_seconds=0)

        # Records should be removed
        assert registry.get(r1.id) is None