"""Agent self-evolution — auto-extract skills from successes, learn from failures.

Triggered at the end of each task run. Analyzes the conversation history
to decide whether to create a skill or record a failure pattern.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .memory import MemoryManager


_EVOLUTION_DIR = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent"))
_EVOLUTION_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class FailurePattern:
    task_type: str
    error_signature: str
    context: str
    resolution: str
    count: int = 1


class EvolutionEngine:
    """Analyze task outcomes and evolve agent capabilities.

    - Successes: auto-extract reusable skills
    - Failures: record patterns to avoid repeated mistakes
    """

    def __init__(self, enabled: bool = False, cache_dir: Optional[Path] = None):
        self.enabled = enabled
        self._dir = cache_dir or _EVOLUTION_DIR
        self.failure_log = self._dir / "failure_patterns.jsonl"
        self.skill_log = self._dir / "auto_skills.jsonl"

    def analyze_run(self, task: str, memory: MemoryManager) -> dict:
        """Analyze a completed run and return evolution actions.

        Returns dict with keys:
        - skill_created: bool
        - failure_recorded: bool
        - actions: list of human-readable descriptions
        """
        if not self.enabled:
            return {"skill_created": False, "failure_recorded": False, "actions": []}

        actions = []
        messages = memory.working_memory

        # Determine outcome
        has_errors = any(
            m.role == "tool" and m.content and "Error:" in m.content
            for m in messages
        )
        tool_calls = sum(1 for m in messages if m.role == "assistant" and m.tool_calls)

        # Success heuristic: multiple tool calls executed without errors
        if tool_calls >= 2 and not has_errors:
            skill = self._try_extract_skill(task, messages)
            if skill:
                actions.append(f"Auto-skill extracted: {skill['name']}")
                return {
                    "skill_created": True,
                    "failure_recorded": False,
                    "actions": actions,
                    "skill": skill,
                }

        # Failure heuristic: errors present or no progress
        if has_errors:
            failure = self._record_failure(task, messages)
            if failure:
                actions.append(f"Failure pattern recorded: {failure['error_signature']}")
                return {
                    "skill_created": False,
                    "failure_recorded": True,
                    "actions": actions,
                    "failure": failure,
                }

        return {"skill_created": False, "failure_recorded": False, "actions": actions}

    def _try_extract_skill(self, task: str, messages: list) -> Optional[dict]:
        """Extract a reusable skill from a successful task.

        Heuristic: look for repeated tool call patterns that could generalize.
        """
        # Extract file operations
        file_ops = []
        for m in messages:
            if m.role == "tool" and m.content:
                # Look for write_file / edit_file success patterns
                if "last_written_file" in str(m.content) or "last_read_file" in str(m.content):
                    continue  # Skip auto-remember entries

        # Build a simple skill from the task pattern
        task_lower = task.lower()

        # Detect skill type from task keywords
        skill_type = None
        if any(k in task_lower for k in ("test", "pytest", "unittest")):
            skill_type = "testing"
        elif any(k in task_lower for k in ("sort", "algorithm", "fibonacci", "quick sort")):
            skill_type = "algorithm"
        elif any(k in task_lower for k in ("api", "rest", "endpoint", "route")):
            skill_type = "api"
        elif any(k in task_lower for k in ("docker", "container", "image")):
            skill_type = "docker"
        elif any(k in task_lower for k in ("git", "commit", "branch", "merge")):
            skill_type = "git"

        if not skill_type:
            return None

        # Generate skill name from task
        keywords = re.findall(r"[a-zA-Z]+", task_lower)
        stopwords = {"the", "a", "an", "is", "are", "was", "to", "and", "or", "in", "on", "at", "for", "with", "of", "from", "by", "as", "it", "this", "that", "write", "create", "build", "make", "implement", "add", "using", "use"}
        skill_words = [w for w in keywords if w not in stopwords and len(w) > 2]
        skill_name = "_".join(skill_words[:4]) or "auto_skill"

        # Build skill content from tool call patterns
        tool_patterns = []
        for m in messages:
            if m.role == "assistant" and m.tool_calls:
                try:
                    tcs = json.loads(m.tool_calls)
                    for tc in tcs:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = fn.get("arguments", "")
                        tool_patterns.append(f"- {name}: {args[:100]}")
                except (json.JSONDecodeError, AttributeError):
                    pass

        if len(tool_patterns) < 2:
            return None

        content = f"""When asked to {task[:100]}, follow this approach:

Key steps:
{chr(10).join(tool_patterns[:5])}

Result: Successful execution pattern.
"""

        skill = {
            "name": f"auto_{skill_name}",
            "description": f"Auto-extracted skill for: {task[:80]}",
            "tags": [skill_type, "auto-generated"],
            "content": content,
        }

        # Log for tracking
        self._append_jsonl(self.skill_log, skill)
        return skill

    def _record_failure(self, task: str, messages: list) -> Optional[dict]:
        """Record a failure pattern for future avoidance."""
        error_msgs = []
        for m in messages:
            if m.role == "tool" and m.content and "Error:" in m.content:
                # Extract error signature (first line of error)
                first_line = m.content.split("\n")[0].strip()
                error_msgs.append(first_line)

        if not error_msgs:
            return None

        # Deduplicate by error signature
        signature = error_msgs[0][:80]

        # Check if this pattern already exists
        existing = self._load_failure_patterns()
        for p in existing:
            if p.error_signature == signature:
                p.count += 1
                self._save_failure_patterns(existing)
                return {"error_signature": signature, "count": p.count, "dedup": True}

        # Extract context: what was the last tool call before error
        last_tool = ""
        for i, m in enumerate(messages):
            if m.role == "assistant" and m.tool_calls:
                try:
                    tcs = json.loads(m.tool_calls)
                    if tcs:
                        last_tool = tcs[0].get("function", {}).get("name", "")
                except (json.JSONDecodeError, AttributeError):
                    pass

        failure = {
            "task_type": task[:50],
            "error_signature": signature,
            "context": f"Failed during {last_tool}",
            "resolution": "",
            "count": 1,
        }

        self._append_jsonl(self.failure_log, failure)
        return failure

    def _append_jsonl(self, path: Path, data: dict):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _load_failure_patterns(self) -> List[FailurePattern]:
        if not self.failure_log.exists():
            return []
        patterns = []
        try:
            with open(self.failure_log, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    # Deduplicate by signature
                    existing = next((p for p in patterns if p.error_signature == data["error_signature"]), None)
                    if existing:
                        existing.count += data.get("count", 1)
                    else:
                        patterns.append(FailurePattern(**data))
        except (json.JSONDecodeError, OSError):
            pass
        return patterns

    def _save_failure_patterns(self, patterns: List[FailurePattern]):
        with open(self.failure_log, "w", encoding="utf-8") as f:
            for p in patterns:
                f.write(json.dumps({
                    "task_type": p.task_type,
                    "error_signature": p.error_signature,
                    "context": p.context,
                    "resolution": p.resolution,
                    "count": p.count,
                }, ensure_ascii=False) + "\n")

    def get_failure_context(self, task: str) -> str:
        """Get relevant failure patterns for a task as context string."""
        patterns = self._load_failure_patterns()
        task_lower = task.lower()
        relevant = [p for p in patterns if p.task_type.lower() in task_lower or task_lower in p.task_type.lower()]
        if not relevant:
            return ""

        lines = ["[Past failures for similar tasks]"]
        for p in relevant[:3]:
            lines.append(f"- {p.error_signature} (occurred {p.count}x)")
            if p.context:
                lines.append(f"  Context: {p.context}")
        return "\n".join(lines)
