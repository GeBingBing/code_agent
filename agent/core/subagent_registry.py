"""SubAgent Registry - 多agent嵌套管理，参考OpenClaw的subagent-registry设计。

提供：
- 树形subagent追踪
- 深度限制（默认5层）
- 生命周期管理（spawn/complete/kill）
- 父子关系查询
"""

import asyncio
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

# Note: ToolResult is imported in sub_agent.py, not here
# Avoid circular imports by not importing ToolResult here


class SubAgentStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class SubAgentRecord:
    """单条subagent记录"""
    id: str
    parent_id: Optional[str]
    label: str
    task: str
    status: SubAgentStatus
    created_at: datetime
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None
    depth: int = 0
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "label": self.label,
            "task": self.task[:50] + "..." if len(self.task) > 50 else self.task,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "depth": self.depth,
        }


class SubAgentRegistry:
    """全局subagent注册表

    类似OpenClaw的subagent-registry，支持：
    - 树形嵌套（parent_id追踪）
    - 深度限制（防止递归爆炸）
    - 生命周期管理（spawn/complete/kill）
    """

    MAX_DEPTH = 5  # 最大嵌套深度

    def __init__(self):
        self._records: Dict[str, SubAgentRecord] = {}
        self._children: Dict[str, List[str]] = {}  # parent_id -> [child_ids]
        self._running_tasks: Dict[str, asyncio.Task] = {}  # run_id -> asyncio.Task
        # Note: asyncio is single-threaded — sync methods (spawn/complete/kill)
        # are atomic between await points, so no explicit lock needed.

    def spawn(
        self,
        parent_id: Optional[str],
        label: str,
        task: str,
        depth: int = 0,
    ) -> SubAgentRecord:
        """派生子agent，返回record。

        Args:
            parent_id: 父agent的run_id（如果为None则是根agent）
            label: 子agent的标签（用于识别）
            task: 子agent的任务描述
            depth: 当前深度（自动计算）

        Returns:
            SubAgentRecord

        Raises:
            ValueError: 超过最大深度限制
        """
        if depth >= self.MAX_DEPTH:
            raise ValueError(f"Max subagent depth {self.MAX_DEPTH} exceeded")

        run_id = str(uuid.uuid4())[:8]

        record = SubAgentRecord(
            id=run_id,
            parent_id=parent_id,
            label=label,
            task=task,
            status=SubAgentStatus.RUNNING,
            created_at=datetime.now(),
            depth=depth,
        )

        self._records[run_id] = record

        # 更新children索引
        if parent_id:
            if parent_id not in self._children:
                self._children[parent_id] = []
            self._children[parent_id].append(run_id)

        return record

    def complete(self, run_id: str, result: str):
        """标记subagent完成。

        Args:
            run_id: subagent的run_id
            result: 执行结果
        """
        if run_id not in self._records:
            return

        record = self._records[run_id]
        record.status = SubAgentStatus.COMPLETED
        record.completed_at = datetime.now()
        record.result = result

        # 清理running task
        if run_id in self._running_tasks:
            del self._running_tasks[run_id]

    def fail(self, run_id: str, error: str):
        """标记subagent失败。

        Args:
            run_id: subagent的run_id
            error: 错误信息
        """
        if run_id not in self._records:
            return

        record = self._records[run_id]
        record.status = SubAgentStatus.FAILED
        record.completed_at = datetime.now()
        record.error = error

        if run_id in self._running_tasks:
            del self._running_tasks[run_id]

    def kill(self, run_id: str) -> bool:
        """杀死运行中的subagent。

        Args:
            run_id: subagent的run_id

        Returns:
            是否成功杀死
        """
        if run_id not in self._records:
            return False

        record = self._records[run_id]

        # 如果正在运行，取消asyncio Task
        if run_id in self._running_tasks:
            self._running_tasks[run_id].cancel()
            del self._running_tasks[run_id]

        record.status = SubAgentStatus.KILLED
        record.completed_at = datetime.now()

        # 递归kill所有子节点
        children = self._children.get(run_id, [])
        for child_id in children:
            self.kill(child_id)

        return True

    def list_children(self, parent_id: str) -> List[SubAgentRecord]:
        """列出直接子节点。

        Args:
            parent_id: 父agent的run_id

        Returns:
            子agent列表
        """
        child_ids = self._children.get(parent_id, [])
        return [self._records[cid] for cid in child_ids if cid in self._records]

    def list_active(self) -> List[SubAgentRecord]:
        """列出所有活跃（running）的subagent。

        Returns:
            活跃subagent列表
        """
        return [
            r for r in self._records.values()
            if r.status == SubAgentStatus.RUNNING
        ]

    def list_all(self) -> List[SubAgentRecord]:
        """列出所有subagent记录。

        Returns:
            所有subagent列表
        """
        return list(self._records.values())

    def get(self, run_id: str) -> Optional[SubAgentRecord]:
        """获取指定run_id的记录。

        Args:
            run_id: subagent的run_id

        Returns:
            记录或None
        """
        return self._records.get(run_id)

    def get_ancestors(self, run_id: str) -> List[SubAgentRecord]:
        """获取指定subagent的所有祖先。

        Args:
            run_id: subagent的run_id

        Returns:
            祖先列表（从父到根）
        """
        ancestors = []
        current = self._records.get(run_id)
        while current and current.parent_id:
            parent = self._records.get(current.parent_id)
            if parent:
                ancestors.append(parent)
                current = parent
            else:
                break
        return ancestors

    def get_tree(self, run_id: str) -> dict:
        """获取指定subagent的完整子树。

        Args:
            run_id: subagent的run_id

        Returns:
            树形结构dict
        """
        record = self._records.get(run_id)
        if not record:
            return {}

        def build_node(r: SubAgentRecord) -> dict:
            children = self.list_children(r.id)
            return {
                "id": r.id,
                "label": r.label,
                "status": r.status.value,
                "depth": r.depth,
                "children": [build_node(c) for c in children]
            }

        return build_node(record)

    def register_task(self, run_id: str, task: asyncio.Task):
        """注册asyncio Task以便管理。

        Args:
            run_id: subagent的run_id
            task: asyncio.Task
        """
        if run_id in self._records:
            self._records[run_id]._task = task

    def set_background(self, run_id: str, is_bg: bool):
        """Mark sub-agent as background task."""
        if run_id in self._records:
            self._records[run_id].metadata["background"] = is_bg
        self._running_tasks[run_id] = task

    def cleanup_completed(self, max_age_seconds: int = 3600):
        """清理超过指定时间的已完成记录。

        Args:
            max_age_seconds: 最大存活时间（秒）
        """
        now = datetime.now()
        to_remove = []

        for run_id, record in self._records.items():
            if record.completed_at:
                age = (now - record.completed_at).total_seconds()
                if age > max_age_seconds:
                    to_remove.append(run_id)

        for run_id in to_remove:
            # Remove from parent's children list to avoid orphaned references
            parent_id = self._records[run_id].parent_id
            if parent_id and parent_id in self._children:
                if run_id in self._children[parent_id]:
                    self._children[parent_id].remove(run_id)
                if not self._children[parent_id]:
                    del self._children[parent_id]

            del self._records[run_id]
            if run_id in self._children:
                del self._children[run_id]


# 全局单例
_registry: Optional[SubAgentRegistry] = None


def get_registry() -> SubAgentRegistry:
    """获取全局SubAgentRegistry单例。"""
    global _registry
    if _registry is None:
        _registry = SubAgentRegistry()
    return _registry


def reset_registry():
    """重置全局registry（用于测试）。"""
    global _registry
    _registry = None