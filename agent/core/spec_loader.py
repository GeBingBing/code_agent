"""Spec loader — parse SPECS.md and provide phase context for the agent.

SPECS.md format:
    ## Phase X: Name (status emoji)
    - ✅ completed  / ⚠️ partial / 🔜 planned / 📋 backlog
    ### P1-1: Sub-phase
    - [ ] Acceptance criterion
    - [x] Done AC

The loader extracts:
- Current active phase (first 🔜 or ⚠️ phase)
- Completed phases summary
- Full spec context for system prompt injection
- Per-phase checklist items (task breakdown)
- Acceptance Criteria (PR-06): structured ACs with id/phase/status

Two parallel APIs are exposed:
1. The legacy `load_spec()` API: SpecContext / SpecPhase / SpecTask — flat
   per-phase task list (used by Phase 10 P1-2 tools).
2. The PR-06 AC-aware API: `load_spec_document()` returns a SpecDocument
   containing SpecPhase objects with structured AcceptanceCriterion entries.
   ACs can be marked done via `mark_ac_done()` and the state is persisted
   to a JSON sidecar file.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── AC-aware structures (PR-06) ─────────────────────────────────────


@dataclass
class AcceptanceCriterion:
    """A single acceptance criterion parsed from SPECS.md.

    IDs are derived from the parent phase and a sequence number, e.g.
    `P1-2-3` (3rd AC in phase P1-2). This is stable across re-parses.
    """

    id: str
    phase_id: str  # e.g. "P1-2"
    description: str
    status: str = "pending"  # pending | in_progress | done | skipped
    verified_at: Optional[str] = None
    verified_by: Optional[str] = None  # "evaluator" | "human" | "agent"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "phase_id": self.phase_id,
            "description": self.description,
            "status": self.status,
            "verified_at": self.verified_at,
            "verified_by": self.verified_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AcceptanceCriterion":
        return cls(
            id=d.get("id", ""),
            phase_id=d.get("phase_id", ""),
            description=d.get("description", ""),
            status=d.get("status", "pending"),
            verified_at=d.get("verified_at"),
            verified_by=d.get("verified_by"),
        )


@dataclass
class ACSpecPhase:
    """A phase or sub-phase, optionally carrying ACs.

    Distinct from the legacy `SpecPhase` (Phase 10 P1-2) which used
    `number` / `name` / `tasks` fields. This class uses string `id` and
    `acceptance_criteria`. The two coexist for backwards compatibility.
    """

    id: str  # e.g. "P0", "P1-2"
    title: str
    status: str  # "completed" | "partial" | "planned" | "backlog"
    acceptance_criteria: List[AcceptanceCriterion] = field(default_factory=list)
    raw_tasks: List[str] = field(default_factory=list)  # non-AC list items

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "acceptance_criteria": [ac.to_dict() for ac in self.acceptance_criteria],
            "raw_tasks": list(self.raw_tasks),
        }

    @property
    def is_active(self) -> bool:
        return self.status in ("partial", "planned")

    @property
    def pending_acs(self) -> List[AcceptanceCriterion]:
        return [ac for ac in self.acceptance_criteria if ac.status in ("pending", "in_progress")]

    @property
    def done_acs(self) -> List[AcceptanceCriterion]:
        return [ac for ac in self.acceptance_criteria if ac.status == "done"]


@dataclass
class SpecDocument:
    """Full parsed view of SPECS.md with ACs."""

    phases: List[ACSpecPhase]
    file_path: Optional[Path] = None
    loaded_at: str = ""
    schema_version: str = "2.0"  # PR-06 marker

    def to_dict(self) -> dict:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "file_path": str(self.file_path) if self.file_path else None,
            "loaded_at": self.loaded_at,
            "schema_version": self.schema_version,
        }

    def get_phase(self, phase_id: str) -> Optional[ACSpecPhase]:
        for p in self.phases:
            if p.id == phase_id:
                return p
        return None

    def get_active_phase(self) -> Optional[ACSpecPhase]:
        for p in self.phases:
            if p.is_active:
                return p
        return None

    def get_unfinished_acs(self, phase_id: Optional[str] = None) -> List[AcceptanceCriterion]:
        if phase_id is None:
            phases = self.phases
        else:
            phase = self.get_phase(phase_id)
            phases = [phase] if phase else []
        out: List[AcceptanceCriterion] = []
        for p in phases:
            out.extend(p.pending_acs)
        return out

    def mark_ac_done(self, ac_id: str, verified_by: str = "agent") -> bool:
        for p in self.phases:
            for ac in p.acceptance_criteria:
                if ac.id == ac_id:
                    ac.status = "done"
                    ac.verified_at = datetime.now().isoformat()
                    ac.verified_by = verified_by
                    return True
        return False

    def progress(self) -> Dict[str, int]:
        total = sum(len(p.acceptance_criteria) for p in self.phases)
        done = sum(len(p.done_acs) for p in self.phases)
        pending = sum(len(p.pending_acs) for p in self.phases)
        return {"total": total, "done": done, "pending": pending}

    def to_prompt(self) -> str:
        """Compact spec view for system prompt / system-reminder injection."""
        if not self.phases:
            return ""
        lines = ["[Spec — Acceptance Criteria]"]
        active = self.get_active_phase()
        if active:
            lines.append(f"Active: {active.id} {active.title} ({active.status})")
            pending = active.pending_acs[:5]
            if pending:
                lines.append("Pending ACs:")
                for ac in pending:
                    lines.append(f"  - [ ] {ac.id}: {ac.description[:80]}")
        prog = self.progress()
        if prog["total"]:
            pct = prog["done"] / prog["total"] * 100
            lines.append(f"Progress: {prog['done']}/{prog['total']} ({pct:.0f}%)")
        return "\n".join(lines)


# ── AC-aware parser (PR-06) ────────────────────────────────────────


# Phase headings: "## Phase 0: Setup ✅" or "### P1-1: Feature A"
_PHASE_RE = re.compile(
    r"^(#{2,3})\s+(?:Phase\s+)?(P?\d+(?:-\d+)?)\s*:\s*(.+?)(?:\s*✅|\s*⚠️|\s*🔜|\s*📋)?\s*$"
)
# AC line: "- [ ] Some description" or "- [x] Done"
_AC_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+?)\s*$")
# Plain list item
_PLAIN_RE = re.compile(r"^-\s+([^-\[]\S.*)$")


def _infer_status(line: str) -> str:
    if "✅" in line:
        return "completed"
    if "⚠️" in line:
        return "partial"
    if "🔜" in line:
        return "planned"
    if "📋" in line:
        return "backlog"
    return "planned"


def parse_spec_document(md_text: str, file_path: Optional[Path] = None) -> SpecDocument:
    """Parse SPECS.md text into a SpecDocument with structured ACs.

    ACs are detected by `- [ ]` / `- [x]` checkboxes that appear under a
    phase heading. Each phase gets a sub-counter for AC IDs, e.g.
    `P0-1-1`, `P0-1-2`, ...

    Plain (non-checkbox) list items are stored in `phase.raw_tasks` and
    do not become ACs — they are descriptive context only.
    """
    phases: List[ACSpecPhase] = []
    current: Optional[ACSpecPhase] = None
    last_phase_heading: Optional[re.Match] = None

    lines = md_text.split("\n")
    for raw_line in lines:
        line = raw_line.rstrip()
        # Phase heading?
        m = _PHASE_RE.match(line)
        if m:
            depth = len(m.group(1))
            raw_id = m.group(2)  # "P0", "1", "P1-1", "1-1"
            title = m.group(3).strip()
            status = _infer_status(line)
            # Normalize id: drop leading "P" then re-add for consistency
            num = raw_id.lstrip("P")
            phase_id = f"P{num}"
            current = ACSpecPhase(id=phase_id, title=title, status=status)
            phases.append(current)
            last_phase_heading = m
            continue
        if current is None:
            continue  # Skip content before the first phase heading
        # AC line?
        m = _AC_RE.match(line)
        if m:
            checked = m.group(1).lower() == "x"
            desc = m.group(2).strip()
            ac_num = len(current.acceptance_criteria) + 1
            ac_id = f"{current.id}-{ac_num}"
            ac = AcceptanceCriterion(
                id=ac_id,
                phase_id=current.id,
                description=desc,
                status="done" if checked else "pending",
                verified_at=datetime.now().isoformat() if checked else None,
                verified_by="human" if checked else None,
            )
            current.acceptance_criteria.append(ac)
            continue
        # Plain list?
        m = _PLAIN_RE.match(line)
        if m:
            current.raw_tasks.append(m.group(1).strip())

    return SpecDocument(
        phases=phases,
        file_path=file_path,
        loaded_at=datetime.now().isoformat(),
    )


def load_spec_document(workspace: Path, cache_path: Optional[Path] = None) -> SpecDocument:
    """Load SPECS.md from workspace as a SpecDocument.

    Args:
        workspace: Workspace root.
        cache_path: Optional JSON cache for AC completion state (defaults
                    to `<workspace>/.spec_ac_state.json`).
    """
    spec_file = workspace / "SPECS.md"
    if not spec_file.exists():
        return SpecDocument(phases=[], file_path=None, loaded_at=datetime.now().isoformat())
    try:
        content = spec_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return SpecDocument(phases=[], file_path=None, loaded_at=datetime.now().isoformat())
    doc = parse_spec_document(content, file_path=spec_file)
    # Apply cached AC state, if any
    cache = cache_path or _default_cache_path(workspace)
    if cache.exists():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            for entry in cached.get("acs", []):
                ac_id = entry.get("id")
                if not ac_id:
                    continue
                for p in doc.phases:
                    for ac in p.acceptance_criteria:
                        if ac.id == ac_id:
                            ac.status = entry.get("status", ac.status)
                            ac.verified_at = entry.get("verified_at", ac.verified_at)
                            ac.verified_by = entry.get("verified_by", ac.verified_by)
                            break
        except (json.JSONDecodeError, OSError):
            pass
    return doc


def save_ac_state(workspace: Path, doc: SpecDocument, cache_path: Optional[Path] = None) -> None:
    """Persist the AC completion state to a JSON sidecar."""
    cache = cache_path or _default_cache_path(workspace)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "saved_at": datetime.now().isoformat(),
        "source": str(doc.file_path) if doc.file_path else None,
        "acs": [
            {
                "id": ac.id,
                "phase_id": ac.phase_id,
                "status": ac.status,
                "verified_at": ac.verified_at,
                "verified_by": ac.verified_by,
            }
            for p in doc.phases
            for ac in p.acceptance_criteria
            if ac.status != "pending"
        ],
    }
    tmp = cache.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        # Fallback: direct write
        cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def mark_ac_done(
    workspace: Path, ac_id: str, verified_by: str = "agent", cache_path: Optional[Path] = None
) -> bool:
    """Mark an AC done in the workspace's spec, persisting the state."""
    doc = load_spec_document(workspace, cache_path=cache_path)
    if doc.mark_ac_done(ac_id, verified_by=verified_by):
        save_ac_state(workspace, doc, cache_path=cache_path)
        return True
    return False


def _default_cache_path(workspace: Path) -> Path:
    """Where the AC completion sidecar lives."""
    override = os.getenv("CODING_AGENT_AC_CACHE")
    if override:
        return Path(override)
    return workspace / ".spec_ac_state.json"


# ── Legacy API (Phase 10 P1-2) ──────────────────────────────────────
# The classes below are kept for backwards compatibility. They are NOT
# dataclass-compatible with SpecPhase/AcceptanceCriterion above (different
# field shapes), so adapters exist where the two views meet.


@dataclass
class SpecTask:
    description: str
    done: bool = False


@dataclass
class SpecPhase:
    number: int
    name: str
    status: str  # completed | partial | planned | backlog
    items: List[str] = field(default_factory=list)
    tasks: List[SpecTask] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status in ("partial", "planned")

    @property
    def completed_tasks(self) -> List[SpecTask]:
        return [t for t in self.tasks if t.done]

    @property
    def pending_tasks(self) -> List[SpecTask]:
        return [t for t in self.tasks if not t.done]


@dataclass
class SpecContext:
    phases: List[SpecPhase]
    active_phase: Optional[SpecPhase] = None
    source_path: str = ""

    def to_prompt(self) -> str:
        """Format spec context for system prompt injection."""
        if not self.phases:
            return ""

        lines = ["[Project spec progress — SPECS.md]"]

        # Active phase
        if self.active_phase:
            lines.append(
                f"Current phase: Phase {self.active_phase.number}: {self.active_phase.name} ({self.active_phase.status})"
            )
            # Include pending tasks for active phase
            pending = self.active_phase.pending_tasks
            if pending:
                lines.append("Pending tasks:")
                for t in pending[:5]:
                    lines.append(f"  - [ ] {t.description}")
            done = self.active_phase.completed_tasks
            if done:
                lines.append("Completed tasks:")
                for t in done[:5]:
                    lines.append(f"  - [x] {t.description}")

        # Completed phases summary
        completed = [p for p in self.phases if p.status == "completed"]
        if completed:
            names = ", ".join(f"P{p.number}:{p.name}" for p in completed[:5])
            lines.append(f"Completed: {names}")

        # Upcoming
        upcoming = [p for p in self.phases if p.status in ("planned", "partial")]
        if upcoming:
            names = ", ".join(f"P{p.number}:{p.name}" for p in upcoming[:3])
            lines.append(f"Upcoming: {names}")

        return "\n".join(lines)

    def get_phase(self, number: int) -> Optional[SpecPhase]:
        """Get phase by number."""
        for p in self.phases:
            if p.number == number:
                return p
        return None

    def all_pending_tasks(self) -> Dict[int, List[SpecTask]]:
        """Return all pending tasks grouped by phase number."""
        return {p.number: p.pending_tasks for p in self.phases if p.pending_tasks}


def load_spec(workspace: Path) -> SpecContext:
    """Parse SPECS.md from workspace root.

    Returns SpecContext with phase information, or empty context if no SPECS.md found.
    """
    spec_file = workspace / "SPECS.md"
    if not spec_file.exists():
        return SpecContext(phases=[], source_path="")

    try:
        content = spec_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return SpecContext(phases=[], source_path="")

    phases = _parse_spec(content)
    active = None
    for p in phases:
        if p.is_active:
            active = p
            break

    return SpecContext(
        phases=phases,
        active_phase=active,
        source_path=str(spec_file),
    )


def _parse_spec(content: str) -> List[SpecPhase]:
    """Parse SPECS.md content into phase list with tasks."""
    phases = []
    # Match "## Phase N: Name" or "### P1-1: Name"
    # Group 1 captures the number (e.g., "0", "1-1", "11")
    # Group 2 captures the name (emoji stripped later)
    phase_pattern = re.compile(
        r"^#{2,3}\s*(?:Phase\s*)?(?:P)?(\d+(?:-\d+)?)\s*:\s*(.+?)(?:\s*✅|\s*⚠️|\s*🔜|\s*📋)?\s*$",
        re.MULTILINE,
    )

    # Find all phase headings and their positions
    matches = list(phase_pattern.finditer(content))

    for i, match in enumerate(matches):
        num_raw = match.group(1)
        name = match.group(2).strip()
        line = match.group(0)

        # Parse number — for P1-1 style, take the first digit (main phase)
        # Skip range phases like "0–8" by checking for non-digit characters beyond simple "N-N" pattern
        try:
            num = int(num_raw.split("-")[0])
        except ValueError:
            continue

        if "✅" in line:
            status = "completed"
        elif "⚠️" in line:
            status = "partial"
        elif "🔜" in line:
            status = "planned"
        elif "📋" in line:
            status = "backlog"
        else:
            status = "planned"

        # Extract tasks between this heading and the next heading
        start_pos = match.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[start_pos:end_pos]

        tasks = _parse_tasks(section)

        phases.append(SpecPhase(number=num, name=name, status=status, tasks=tasks))

    return phases


def _parse_tasks(section: str) -> List[SpecTask]:
    """Parse checklist items from a section.

    Supports:
    - [x] Done task
    - [ ] Pending task
    - Plain list item (treats as pending checklist)
    """
    # Truncate at any sub-heading to avoid picking up tasks from later phases
    heading_match = re.search(r"^\s*#{1,6}\s", section, re.MULTILINE)
    if heading_match:
        section = section[: heading_match.start()]

    tasks = []
    # Match checklist or plain list items
    task_pattern = re.compile(r"^\s*[-*]\s*(?:\[([ xX])\])?\s*(.+)$", re.MULTILINE)
    for match in task_pattern.finditer(section):
        checkbox = match.group(1)
        description = match.group(2).strip()
        # Stop at empty lines or sub-headings (belt and suspenders)
        if not description or description.startswith("#"):
            break
        done = checkbox in ("x", "X")
        tasks.append(SpecTask(description=description, done=done))
    return tasks


def mark_task_done(workspace: Path, phase_number: int, task_description: str) -> bool:
    """Mark a specific task as done in SPECS.md by updating - [ ] to - [x].

    Args:
        workspace: Workspace root path
        phase_number: Phase number (e.g., 1 for P1-1)
        task_description: Task description to match (partial match supported)

    Returns:
        True if a task was found and marked, False otherwise
    """
    spec_file = workspace / "SPECS.md"
    if not spec_file.exists():
        return False

    content = spec_file.read_text(encoding="utf-8")

    # Build regex to find the phase heading
    phase_pattern = re.compile(
        rf"^(#{{2,3}}\s*(?:Phase\s*)?(?:P)?{phase_number}(?:-\d+)?\s*:.*)$", re.MULTILINE
    )
    match = phase_pattern.search(content)
    if not match:
        return False

    phase_start = match.start()
    # Find next phase heading or end of file
    next_phase = phase_pattern.search(content, phase_start + 1)
    phase_end = next_phase.start() if next_phase else len(content)
    phase_section = content[phase_start:phase_end]

    # Find the task in this section and mark it done
    # Match the task line with - [ ] or plain -
    task_regex = re.compile(
        rf"^(\s*[-*]\s*)(?:\[\s*\])?\s*({re.escape(task_description)}.*?)$", re.MULTILINE
    )

    updated_section, count = task_regex.subn(r"\1[x] \2", phase_section, count=1)
    if count == 0:
        # Try partial match
        task_regex = re.compile(
            rf"^(\s*[-*]\s*)(?:\[\s*\])?\s*(.*{re.escape(task_description[:20])}.*?)$", re.MULTILINE
        )
        updated_section, count = task_regex.subn(r"\1[x] \2", phase_section, count=1)

    if count == 0:
        return False

    new_content = content[:phase_start] + updated_section + content[phase_end:]
    spec_file.write_text(new_content, encoding="utf-8")
    return True


def verify_against_spec(workspace: Path, implementation_summary: str) -> dict:
    """Verify implementation against SPECS.md checklist.

    Returns a report with:
    - Missing tasks (in spec but not mentioned in implementation)
    - Completed tasks
    - Coverage percentage
    """
    ctx = load_spec(workspace)
    if not ctx.phases:
        return {"error": "No SPECS.md found", "coverage": 0.0}

    summary_lower = implementation_summary.lower()
    report = {
        "phases_checked": [],
        "pending_tasks": [],
        "completed_tasks": [],
        "coverage": 0.0,
    }

    total_tasks = 0
    matched_tasks = 0

    for phase in ctx.phases:
        if not phase.tasks:
            continue
        report["phases_checked"].append(f"P{phase.number}: {phase.name}")
        for task in phase.tasks:
            total_tasks += 1
            if task.done:
                report["completed_tasks"].append(f"P{phase.number}: {task.description}")
                matched_tasks += 1
            else:
                # Heuristic: check if task keywords appear in implementation summary
                keywords = [w for w in task.description.lower().split() if len(w) > 3]
                if keywords and any(kw in summary_lower for kw in keywords[:3]):
                    matched_tasks += 1
                else:
                    report["pending_tasks"].append(f"P{phase.number}: {task.description}")

    report["coverage"] = matched_tasks / total_tasks if total_tasks > 0 else 1.0
    return report
