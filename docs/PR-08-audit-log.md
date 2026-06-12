# PR-08: 不可变审计日志

> 关联：SPECS.md Phase 13-4 | 状态：✅ 已实施 | 决策：已确认
> 依据：[docs/1.md §8 安全审计](../1.md) | [docs/参考.md 纵深防御 ClawAegis](../参考.md)

---

## 决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 存储格式 | JSONL append-only | 行式追加，简单，grep 友好 |
| 路径 | `~/.coding-agent/audit/{date}.jsonl` | 按天滚动 |
| 保留期 | 30 天后归档（tar.gz 到 `archive/`） | 平衡存储和合规 |
| 不变性 | 无 delete API；rotate 只能整体替换 | 防篡改 |
| 写入时机 | 通过 `before_tool_execution` / `after_tool_execution` Hook（PR-01） | 自动且强制 |
| 隐私 | args 和 result 只存 hash + 长度，不存明文 | 避免泄露敏感信息 |

---

## 现状 / 目标

**现状**：
- 无审计日志
- agent 行为不可追溯
- 出现问题时无法复盘"agent 调过哪些工具、参数是什么"
- 权限拒绝的原因没有持久化记录

**目标**（1.md §8）：
> **安全审计**：所有 Agent 行为生成不可变审计日志

每条记录：
```json
{
  "ts": "2026-06-06T10:23:45.123Z",
  "session_id": "20260606-102345-a3f2",
  "agent_id": "main" | "sub-uuid" | "orchestrator",
  "action": "tool_call" | "tool_result" | "permission_check" | "state_transition",
  "tool": "write_file",
  "args_hash": "sha256:abc123...",
  "args_size": 256,
  "result_hash": "sha256:def456...",
  "result_size": 1024,
  "permission_decision": "allow" | "ask" | "deny",
  "duration_ms": 12.5,
  "error": null | "permission denied"
}
```

---

## 设计

### AuditLogger

```python
# agent/core/audit_log.py (新文件)

import json
import hashlib
import time
from pathlib import Path
from datetime import datetime, timedelta


class AuditLogger:
    """Append-only JSONL audit log. Cannot delete entries, only rotate."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, log_dir: Path = Path.home() / ".coding-agent" / "audit"):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = log_dir / "archive"
        self.archive_dir.mkdir(exist_ok=True)

    def _get_today_file(self) -> Path:
        return self.log_dir / f"{datetime.now().date().isoformat()}.jsonl"

    def log(self, record: dict) -> None:
        """Append a record. Atomic via single-line write."""
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "ts": datetime.utcnow().isoformat() + "Z",
            **record,
        }
        # 隐私：args 和 result 只存 hash + size
        if "args" in record:
            args = record.pop("args")
            record["args_hash"] = "sha256:" + hashlib.sha256(
                json.dumps(args, sort_keys=True).encode()
            ).hexdigest()[:32]
            record["args_size"] = len(json.dumps(args))
        if "result" in record:
            result = record.pop("result")
            record["result_hash"] = "sha256:" + hashlib.sha256(
                str(result).encode()
            ).hexdigest()[:32]
            record["result_size"] = len(str(result))
        # Append
        with self._get_today_file().open("a") as f:
            f.write(json.dumps(record) + "\n")

    def query(
        self,
        start: str = None,        # ISO date "2026-06-01"
        end: str = None,
        agent_id: str = None,
        action: str = None,
        tool: str = None,
    ) -> list[dict]:
        """Query audit records with filters."""
        results = []
        for log_file in sorted(self.log_dir.glob("*.jsonl")):
            for line in log_file.open():
                rec = json.loads(line)
                if start and rec["ts"] < start:
                    continue
                if end and rec["ts"] > end:
                    continue
                if agent_id and rec.get("agent_id") != agent_id:
                    continue
                if action and rec.get("action") != action:
                    continue
                if tool and rec.get("tool") != tool:
                    continue
                results.append(rec)
        return results

    def rotate(self, retention_days: int = 30) -> int:
        """Archive logs older than retention_days. Returns count archived."""
        cutoff = datetime.now() - timedelta(days=retention_days)
        count = 0
        for log_file in self.log_dir.glob("*.jsonl"):
            file_date = datetime.fromisoformat(log_file.stem)
            if file_date < cutoff:
                archive_path = self.archive_dir / f"{log_file.name}.tar.gz"
                # tar gzip
                import tarfile
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(log_file, arcname=log_file.name)
                log_file.unlink()
                count += 1
        return count

    def stats(self) -> dict:
        """Quick stats for /status command."""
        total = 0
        by_action = {}
        for log_file in self.log_dir.glob("*.jsonl"):
            for line in log_file.open():
                total += 1
                rec = json.loads(line)
                by_action[rec.get("action", "unknown")] = by_action.get(rec.get("action", "unknown"), 0) + 1
        return {"total_entries": total, "by_action": by_action}
```

### Engine Hook 集成

```python
# agent/core/engine.py 修改

class AgentEngine:
    def __init__(self, ...):
        self.audit = AuditLogger()
        # Hook: before_tool_execution → audit permission check
        self.hooks.register("before_tool_execution", self._audit_before_tool)
        # Hook: after_tool_execution → audit result
        self.hooks.register("after_tool_execution", self._audit_after_tool)

    async def _audit_before_tool(self, payload):
        tool_name = payload["tool"]
        args = payload["args"]
        # Permission check happens first
        decision = self.permissions.check(tool_name, args)
        self.audit.log({
            "session_id": self.session_id,
            "agent_id": "main",
            "action": "tool_call",
            "tool": tool_name,
            "args": args,
            "permission_decision": decision.value,
        })
        if decision == PermissionDecision.DENY:
            raise PermissionDenied(f"{tool_name} denied by policy")

    async def _audit_after_tool(self, payload):
        result = payload.get("result")
        error = payload.get("error")
        duration_ms = (time.time() - payload["start_ts"]) * 1000
        self.audit.log({
            "session_id": self.session_id,
            "agent_id": "main",
            "action": "tool_result",
            "tool": payload["tool"],
            "result": str(result) if result else None,
            "duration_ms": duration_ms,
            "error": str(error) if error else None,
        })
```

### 工具：审计查询

```python
# agent/tools/audit.py (新文件)

class AuditQueryTool(BaseTool):
    name = "audit_query"
    description = "Query audit log with filters. Returns matching records."

    async def execute(
        self,
        start: str = None,
        end: str = None,
        agent_id: str = None,
        action: str = None,
        tool: str = None,
        limit: int = 100,
        **kwargs,
    ) -> ToolResult:
        results = self.audit.query(start, end, agent_id, action, tool)
        return ToolResult(output=json.dumps(results[:limit], indent=2))
```

### Cron 自动归档

```python
# agent/core/audit_log.py 增加

def setup_audit_cron(audit: AuditLogger, retention_days: int = 30):
    """Register a daily task to rotate old audit logs."""
    async def daily_rotate():
        while True:
            count = audit.rotate(retention_days)
            if count:
                logger.info("Archived %d old audit logs", count)
            # Sleep until next midnight
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            await asyncio.sleep((tomorrow - now).total_seconds())
    asyncio.create_task(daily_rotate())
```

---

## 实现清单

| 文件 | 改动 |
|------|------|
| `agent/core/audit_log.py` | **新建** — AuditLogger + 隐私 hash + rotate |
| `agent/core/engine.py` | 集成 audit logger；hook before_tool_execution + after_tool_execution |
| `agent/tools/audit.py` | **新建** — AuditQueryTool |
| `agent/tools/__init__.py` | 注册 audit_query |
| `agent/commands/builtin.py` | `/audit stats` / `/audit query` 命令 |
| `ui/cli.py` | `/status` 增加 audit stats |
| `tests/test_audit_log.py` | **新建** — 写入、查询、过滤、隐私 hash、rotate |
| `tests/test_engine_audit.py` | **新建** — hook 自动记录所有 tool_call |

---

## 验收标准

- [ ] `AuditLogger.log(record)` 追加一行 JSON 到 `~/.coding-agent/audit/{date}.jsonl`
- [ ] args 和 result 不存明文，只存 `sha256:` 前缀 hash + size
- [ ] `query(start, end, agent_id, action, tool)` 返回过滤后的记录
- [ ] `rotate(30)` 归档 30 天前的日志到 `archive/`
- [ ] 无 delete API（只能整体 rotate）
- [ ] engine `before_tool_execution` hook 自动记录 permission decision
- [ ] engine `after_tool_execution` hook 自动记录 result + duration
- [ ] `audit_query` 工具注册成功，LLM 可调用
- [ ] `/audit stats` 显示总记录数 + 按 action 分组
- [ ] 现有 398+ 测试不回归

---

## 实施顺序

```
Step 1: agent/core/audit_log.py              (新文件，2h)
Step 2: tests/test_audit_log.py              (新文件，1h)
Step 3: agent/core/engine.py 集成             (改文件，1.5h)
Step 4: agent/tools/audit.py                 (新文件，0.5h)
Step 5: tests/test_engine_audit.py           (新文件，0.5h)
Step 6: agent/commands/builtin.py            (改文件，0.5h)
Step 7: ui/cli.py                            (改文件，0.5h)
Step 8: pytest tests/ 验证                    (0.5h)
```

总工作量：~7h

**前置依赖**：PR-01（Hook 系统）

---

## 与其他 PR 的关系

- 与 PR-01 Hook：审计是 hook 的核心 consumer
- 与 PR-07 Orchestrator：所有子 agent 行为进入同一份 audit
- 与 PR-09 Evaluator：Evaluator 从 audit 提取数据做评分
- 与 PR-10 OpenTelemetry：OTel 关注"性能/追踪"，audit 关注"合规/取证"——两者并存
- 与 PR-11 Dual-agent review：双 Agent 互审的决策进入 audit

---

## 实现参考

| 文件 | 关键符号 |
|------|----------|
| `agent/core/audit_log.py` | `AuditLog.append()` / `AuditLog.query()` — append-only JSONL 写入器 |
| 路径 | `~/.coding-agent/audit/{date}.jsonl`（按天滚动） |
| 记录字段 | `{ts, session_id, agent_id, action, tool, args_hash, result_hash, permission_decision}` |
| 工具 | `audit_query` 支持时间范围 + agent_id 过滤 |
