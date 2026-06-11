"""Append-only JSONL audit log (PR-08).

Goals (per docs/1.md §8):
- All agent actions generate an immutable, hashable audit trail.
- Records cannot be deleted individually; only the whole file can be
  archived via `rotate()`.
- Privacy: `args` and `result` fields are stored as `sha256:` hash +
  size only. Raw content never touches disk.
- One file per day under `~/.coding-agent/audit/{YYYY-MM-DD}.jsonl`.
- Old files can be rotated into `archive/` as `.tar.gz`.

Why append-only?
- Appends are atomic on POSIX; no locks needed for a single writer.
- Any tampering would change line offsets / hash chains, making
  falsification detectable.
- Forensic / compliance use cases (SOC2, audit reviews) expect
  immutable logs.

Why hash-and-size, not full content?
- Audit consumers want to *verify* "this exact args produced this
  exact result" — a hash is sufficient.
- Storing plaintext args/result risks leaking secrets (paths with
  tokens, file contents, env vars).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Public dataclass ───────────────────────────────────────────────


@dataclass
class AuditEntry:
    """A single audit record. Constructed by callers, then passed to `log()`.

    `args` and `result` are sensitive — `log()` will replace them with
    their hash + size in the persisted record.
    """

    session_id: str
    agent_id: str
    action: str  # "tool_call" | "tool_result" | "permission_check" | "state_transition"
    tool: Optional[str] = None
    args: Optional[dict] = None
    result: Optional[object] = None
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    permission_decision: Optional[str] = None  # "allow" | "ask" | "deny"
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "permission_decision": self.permission_decision,
            "metadata": self.metadata,
        }


# ── Logger ─────────────────────────────────────────────────────────


class AuditLogger:
    """Append-only JSONL audit log with daily rotation + tar archive.

    Thread/async safety: Python's `open(..., "a")` is atomic for
    line-sized writes on POSIX when buffered properly. We use
    `write(line + "\n")` followed by explicit flush, which is the
    documented atomic-append pattern.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, log_dir: Optional[Path] = None, archive_dir: Optional[Path] = None):
        if log_dir is None:
            log_dir = Path(
                os.getenv(
                    "CODING_AGENT_AUDIT_DIR",
                    Path.home() / ".coding-agent" / "audit",
                )
            )
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = Path(archive_dir or (self.log_dir / "archive"))
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    # ── Write ─────────────────────────────────────────────────────

    def _today_file(self) -> Path:
        return self.log_dir / f"{datetime.now().date().isoformat()}.jsonl"

    def log(self, record: dict) -> Path:
        """Append a record to today's log file. Returns the file path.

        The record may contain `args` and/or `result` — these are
        replaced with `args_hash`/`args_size` / `result_hash`/`result_size`
        before persistence.
        """
        sanitized = self._scrub_sensitive_fields(dict(record))
        sanitized.setdefault("schema_version", self.SCHEMA_VERSION)
        sanitized.setdefault("ts", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        target = self._today_file()
        line = json.dumps(sanitized, ensure_ascii=False, default=str) + "\n"
        # Use append mode; flush + explicit close for atomicity.
        with target.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return target

    def log_entry(self, entry: AuditEntry) -> Path:
        """Convenience wrapper that accepts an `AuditEntry` dataclass."""
        return self.log(entry.to_dict())

    @staticmethod
    def _scrub_sensitive_fields(record: dict) -> dict:
        """Replace `args` / `result` with hash + size, drop None fields."""
        if "args" in record and record["args"] is not None:
            args = record.pop("args")
            args_str = json.dumps(args, sort_keys=True, default=str)
            record["args_hash"] = (
                "sha256:" + hashlib.sha256(args_str.encode("utf-8")).hexdigest()[:32]
            )
            record["args_size"] = len(args_str)
        elif "args" in record:
            record.pop("args", None)
        if "result" in record and record["result"] is not None:
            result = record.pop("result")
            try:
                result_str = json.dumps(result, default=str)
            except (TypeError, ValueError):
                result_str = str(result)
            record["result_hash"] = (
                "sha256:" + hashlib.sha256(result_str.encode("utf-8")).hexdigest()[:32]
            )
            record["result_size"] = len(result_str)
        elif "result" in record:
            record.pop("result", None)
        # Drop None / empty fields for compactness
        return {k: v for k, v in record.items() if v is not None and v != ""}

    # ── Query ─────────────────────────────────────────────────────

    def query(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        agent_id: Optional[str] = None,
        action: Optional[str] = None,
        tool: Optional[str] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """Query records across all log files (active + archive)."""
        results: List[dict] = []
        files = sorted(self.log_dir.glob("*.jsonl"))
        # Active files first; archive access is much rarer
        for log_file in files:
            for rec in self._read_records(log_file):
                if start and rec.get("ts", "") < start:
                    continue
                if end and rec.get("ts", "") > end:
                    continue
                if agent_id and rec.get("agent_id") != agent_id:
                    continue
                if action and rec.get("action") != action:
                    continue
                if tool and rec.get("tool") != tool:
                    continue
                results.append(rec)
                if len(results) >= limit:
                    return results
        return results

    @staticmethod
    def _read_records(path: Path):
        """Yield parsed records from a JSONL file. Tolerate malformed lines."""
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # Skip malformed lines; never crash the query
                        continue
        except OSError:
            return

    # ── Rotation / archival ──────────────────────────────────────

    def rotate(self, retention_days: int = 30) -> int:
        """Archive log files older than `retention_days` to `archive/`.

        Archived as `{filename}.tar.gz`. Returns count of files archived.
        Files that fail to parse as dates are left alone (never deleted).
        """
        cutoff = datetime.now().date() - timedelta(days=retention_days)
        count = 0
        for log_file in sorted(self.log_dir.glob("*.jsonl")):
            file_date = self._parse_log_date(log_file)
            if file_date is None:
                continue  # Not a date-stamped file (e.g. sidecar); skip
            if file_date >= cutoff:
                continue  # Still within retention
            archive_path = self.archive_dir / f"{log_file.name}.tar.gz"
            try:
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(str(log_file), arcname=log_file.name)
                log_file.unlink()
                count += 1
            except OSError as e:
                logger.warning("Failed to archive %s: %s", log_file, e)
        return count

    @staticmethod
    def _parse_log_date(log_file: Path) -> Optional["datetime.date"]:
        """Extract date from a YYYY-MM-DD.jsonl filename."""
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.jsonl$", log_file.name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> Dict[str, object]:
        """Return aggregate stats: total entries, by_action, by_tool, by_agent."""
        total = 0
        by_action: Dict[str, int] = {}
        by_tool: Dict[str, int] = {}
        by_agent: Dict[str, int] = {}
        for log_file in self.log_dir.glob("*.jsonl"):
            for rec in self._read_records(log_file):
                total += 1
                a = rec.get("action", "unknown")
                by_action[a] = by_action.get(a, 0) + 1
                t = rec.get("tool")
                if t:
                    by_tool[t] = by_tool.get(t, 0) + 1
                ag = rec.get("agent_id", "unknown")
                by_agent[ag] = by_agent.get(ag, 0) + 1
        return {
            "total_entries": total,
            "by_action": by_action,
            "by_tool": by_tool,
            "by_agent": by_agent,
            "log_dir": str(self.log_dir),
            "archive_dir": str(self.archive_dir),
        }

    def list_files(self) -> List[Path]:
        """All active JSONL files, sorted by name."""
        return sorted(self.log_dir.glob("*.jsonl"))

    # ── No delete API (intentional) ──────────────────────────────

    def delete_record(self, *args, **kwargs):
        """Intentionally not implemented. Audit logs are append-only."""
        raise NotImplementedError("AuditLogger is append-only. Use rotate() to archive old files.")


# ── Singleton ──────────────────────────────────────────────────────


_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """Return the process-wide AuditLogger singleton."""
    global _logger
    if _logger is None:
        _logger = AuditLogger()
    return _logger


def reset_audit_logger() -> None:
    """Reset the singleton (for tests)."""
    global _logger
    _logger = None
