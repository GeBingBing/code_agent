"""A/B testing framework (PR-12).

Allows controlled comparison of prompt variants, skill versions, and tool
default parameters without forcing a one-shot rollout. Each experiment
defines 2+ variants, hashes a stable user identifier into a bucket, and
records task-completion observations. Once `min_samples` is reached and
a winner is statistically meaningful (>=5% success-rate difference),
the experiment can be `conclude()`'d — the winning variant is
"promoted" (its config copied to a stable location).

Why an external JSONL store?
- We want to be able to read observations from outside the running
  process (analysis scripts, dashboards) without going through Python.
- JSONL is append-only and human-readable, matching the same shape
  the audit log uses (PR-08).
- `experiment.json` is the source of truth for the experiment
  definition; observations go in their own file so the manager can
  stream records as the log grows.

Why hash-based bucketing?
- A user always sees the same variant for a given experiment — that
  eliminates variance from users bouncing between treatments.
- Hashing is deterministic; the bucketing function survives restarts.
- Trivially extensible: 3+ variants fall out of the cumulative-weight
  loop with no code change.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple


# ── Enums ───────────────────────────────────────────────────────────


class ExperimentStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ExperimentTarget(Enum):
    """What aspect of the system is being varied."""
    SYSTEM_PROMPT = "system_prompt"
    SKILL_PROMPT = "skill_prompt"
    TOOL_DEFAULT = "tool_default"
    USER_REMINDER = "user_reminder"


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class ExperimentVariant:
    """A single treatment within an experiment.

    `config` is opaque to the manager — the engine knows how to
    interpret it based on `Experiment.target`. Examples:
      - {"new_content": "..."} for prompt changes
      - {"tool_name": {"param": value}} for tool defaults
      - {"skill_id": "v2", "override": "..."} for skill swaps
    """
    id: str                        # "A" or "B" (or "control" / "treatment")
    name: str                      # human-readable
    config: dict
    weight: float = 1.0            # traffic weight, default uniform

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "config": self.config,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentVariant":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            config=d.get("config", {}),
            weight=float(d.get("weight", 1.0)),
        )


@dataclass
class Experiment:
    id: str
    name: str
    description: str
    target: str                    # one of ExperimentTarget values
    target_key: str                # section name, skill id, or tool+param
    variants: List[ExperimentVariant]
    status: str = ExperimentStatus.RUNNING.value
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    winner: str = ""               # variant id of the winner
    min_samples: int = 50          # per-variant floor
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "target_key": self.target_key,
            "variants": [v.to_dict() for v in self.variants],
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "winner": self.winner,
            "min_samples": self.min_samples,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Experiment":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            target=d.get("target", ExperimentTarget.SYSTEM_PROMPT.value),
            target_key=d.get("target_key", ""),
            variants=[ExperimentVariant.from_dict(v) for v in d.get("variants", [])],
            status=d.get("status", ExperimentStatus.RUNNING.value),
            created_at=d.get("created_at", ""),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            winner=d.get("winner", ""),
            min_samples=int(d.get("min_samples", 50)),
            metadata=d.get("metadata", {}),
        )

    def variant_by_id(self, vid: str) -> Optional[ExperimentVariant]:
        for v in self.variants:
            if v.id == vid:
                return v
        return None


@dataclass
class ExperimentObservation:
    """A single recorded outcome from a run that participated in an experiment."""
    experiment_id: str
    variant_id: str
    user_id: str
    task: str
    success: bool
    token_input: int
    token_output: int
    duration_ms: float
    user_rating: Optional[int] = None  # 1-5
    ts: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "experiment_id": self.experiment_id,
            "variant_id": self.variant_id,
            "user_id": self.user_id,
            "task": self.task,
            "success": self.success,
            "token_input": self.token_input,
            "token_output": self.token_output,
            "duration_ms": self.duration_ms,
            "user_rating": self.user_rating,
            "ts": self.ts,
        }
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentObservation":
        return cls(
            experiment_id=d["experiment_id"],
            variant_id=d["variant_id"],
            user_id=d.get("user_id", ""),
            task=d.get("task", ""),
            success=bool(d.get("success", False)),
            token_input=int(d.get("token_input", 0)),
            token_output=int(d.get("token_output", 0)),
            duration_ms=float(d.get("duration_ms", 0.0)),
            user_rating=(int(d["user_rating"]) if d.get("user_rating") is not None else None),
            ts=d.get("ts", ""),
            metadata=d.get("metadata", {}),
        )


# ── Analysis result ────────────────────────────────────────────────


@dataclass
class ExperimentAnalysis:
    """Result of analyzing an experiment."""
    status: str                      # "no_data" | "insufficient_samples" | "analyzed"
    results: dict = field(default_factory=dict)        # variant_id -> metrics dict
    winner: str = ""                 # "A" | "B" | "tie" | ""
    have: dict = field(default_factory=dict)           # variant_id -> sample count
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "results": self.results,
            "winner": self.winner,
            "have": self.have,
            "details": self.details,
        }


# ── Manager ────────────────────────────────────────────────────────


class ABTestManager:
    """Manages A/B test experiments: creation, bucketing, observation,
    analysis, and conclusion (winner promotion)."""

    # Default threshold: winner's success_rate must be at least this many
    # percentage points ahead of the other variant.
    DEFAULT_WINNER_DELTA = 0.05  # 5%

    def __init__(self, exp_dir: Optional[Path] = None):
        if exp_dir is None:
            exp_dir = Path(
                os.getenv(
                    "CODING_AGENT_EXPERIMENTS_DIR",
                    Path.home() / ".coding-agent" / "experiments",
                )
            )
        self.exp_dir = Path(exp_dir)
        self.exp_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict = {}
        self._load_all()

    # ── Experiment lifecycle ────────────────────────────────────

    def _load_all(self) -> None:
        for f in self.exp_dir.glob("*/experiment.json"):
            try:
                exp = Experiment.from_dict(json.loads(f.read_text(encoding="utf-8")))
                self._cache[exp.id] = exp
            except Exception:
                # Don't let a malformed experiment block loading the rest
                pass

    def create(self, exp: Experiment) -> Experiment:
        """Persist a new experiment. Returns the saved copy."""
        if not exp.id:
            exp.id = f"exp_{uuid.uuid4().hex[:8]}"
        if not exp.variants or len(exp.variants) < 2:
            raise ValueError("Experiment must have at least 2 variants")
        # Auto-assign A/B IDs if not provided
        for i, v in enumerate(exp.variants):
            if not v.id:
                v.id = chr(ord("A") + i)
        # Validate unique variant IDs
        ids = [v.id for v in exp.variants]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Variant IDs must be unique; got {ids}")
        exp.created_at = datetime.now().isoformat()
        if not exp.started_at:
            exp.started_at = exp.created_at
        self._save(exp)
        self._cache[exp.id] = exp
        return exp

    def list(self) -> List[Experiment]:
        return list(self._cache.values())

    def get(self, exp_id: str) -> Optional[Experiment]:
        return self._cache.get(exp_id)

    def abandon(self, exp_id: str) -> Optional[Experiment]:
        """Mark a running experiment as abandoned (no winner)."""
        exp = self._cache.get(exp_id)
        if exp is None or exp.status != ExperimentStatus.RUNNING.value:
            return exp
        exp.status = ExperimentStatus.ABANDONED.value
        exp.ended_at = datetime.now().isoformat()
        self._save(exp)
        return exp

    # ── Variant assignment ─────────────────────────────────────

    def assign_variant(self, exp_id: str, user_id: str) -> Optional[ExperimentVariant]:
        """Return the variant this user should see for this experiment.

        Hashing strategy: SHA-256 of "{exp_id}:{user_id}", first 8 hex
        chars mod 100 → uniform 0-99 bucket. Cumulative weights pick
        the variant. Same user_id always returns the same variant for
        a given exp_id (within a running experiment).

        If the experiment is no longer running, return the winner
        (control) — so all subsequent users see the chosen treatment.
        """
        exp = self._cache.get(exp_id)
        if exp is None:
            return None
        # Concluded experiments serve the winner
        if exp.status != ExperimentStatus.RUNNING.value:
            if exp.winner:
                return exp.variant_by_id(exp.winner)
            return exp.variants[0]
        # Hash-based bucketing
        h = int(
            hashlib.sha256(f"{exp_id}:{user_id}".encode("utf-8")).hexdigest()[:8],
            16
        )
        bucket = h % 100
        total_weight = sum(v.weight for v in exp.variants) or 1.0
        cumulative = 0.0
        for v in exp.variants:
            cumulative += (v.weight / total_weight) * 100.0
            if bucket < cumulative:
                return v
        return exp.variants[-1]

    # ── Observation recording ──────────────────────────────────

    def record_observation(self, obs: ExperimentObservation) -> Path:
        """Append a single observation to the experiment's JSONL log."""
        if not obs.ts:
            obs.ts = datetime.now().isoformat()
        exp_dir = self.exp_dir / obs.experiment_id
        exp_dir.mkdir(parents=True, exist_ok=True)
        log_file = exp_dir / "observations.jsonl"
        line = json.dumps(obs.to_dict(), ensure_ascii=False, default=str) + "\n"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
        return log_file

    def observations(self, exp_id: str) -> List[ExperimentObservation]:
        """Read all observations for an experiment."""
        log_file = self.exp_dir / exp_id / "observations.jsonl"
        if not log_file.exists():
            return []
        out: List[ExperimentObservation] = []
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(ExperimentObservation.from_dict(json.loads(line)))
            except Exception:
                continue
        return out

    # ── Analysis ──────────────────────────────────────────────

    def analyze(
        self,
        exp_id: str,
        min_samples: Optional[int] = None,
        winner_delta: Optional[float] = None,
    ) -> ExperimentAnalysis:
        """Aggregate observations and decide a winner.

        Rules:
        - If both variants have < min_samples → "insufficient_samples"
        - Compute success_rate, avg_tokens, avg_duration, avg_rating per variant
        - Winner is the variant with success_rate >= winner_delta higher
          than the other; otherwise "tie"
        """
        exp = self._cache.get(exp_id)
        if exp is None:
            return ExperimentAnalysis(
                status="not_found",
                details=f"Experiment {exp_id!r} not found",
            )
        obs_list = self.observations(exp_id)
        if not obs_list:
            return ExperimentAnalysis(status="no_data")
        by_variant: dict = defaultdict(list)
        for o in obs_list:
            by_variant[o.variant_id].append(o)
        have = {vid: len(v) for vid, v in by_variant.items()}
        min_n = min_samples if min_samples is not None else exp.min_samples
        # Need at least 2 variants to compare
        if len(by_variant) < 2:
            return ExperimentAnalysis(
                status="insufficient_variants",
                have=have,
                details="Need observations for at least 2 variants",
            )
        # Per-variant sample floor
        for vid, items in by_variant.items():
            if len(items) < min_n:
                return ExperimentAnalysis(
                    status="insufficient_samples",
                    have=have,
                    details=f"Variant {vid!r} has {len(items)} samples, "
                            f"need {min_n}",
                )
        # Compute metrics
        results: dict = {}
        for vid, items in by_variant.items():
            n = len(items)
            success_count = sum(1 for o in items if o.success)
            total_tokens = sum(o.token_input + o.token_output for o in items)
            total_dur = sum(o.duration_ms for o in items)
            ratings = [o.user_rating for o in items if o.user_rating is not None]
            results[vid] = {
                "n": n,
                "success_rate": success_count / n if n else 0.0,
                "avg_tokens": total_tokens / n if n else 0.0,
                "avg_duration_ms": total_dur / n if n else 0.0,
                "avg_rating": (sum(ratings) / len(ratings)) if ratings else None,
                "success_count": success_count,
            }
        # Winner decision: simple delta on success_rate
        delta = winner_delta if winner_delta is not None else self.DEFAULT_WINNER_DELTA
        # If we have A and B specifically, use them; else pick top-2 by sample size
        variant_ids = sorted(results.keys())
        if "A" in results and "B" in results:
            a, b = results["A"], results["B"]
        else:
            # Sort by sample size; compare top two
            sorted_by_n = sorted(variant_ids, key=lambda v: -results[v]["n"])
            a, b = results[sorted_by_n[0]], results[sorted_by_n[1]]
            variant_ids = sorted_by_n[:2]
        diff = b["success_rate"] - a["success_rate"]
        if abs(diff) >= delta:
            # Whichever is higher — but attribute correctly when A/B are inverted
            if "A" in results and "B" in results:
                winner = "B" if diff > 0 else "A"
            else:
                winner = variant_ids[1] if diff > 0 else variant_ids[0]
        else:
            winner = "tie"
        return ExperimentAnalysis(
            status="analyzed",
            results=results,
            winner=winner,
            have=have,
        )

    # ── Conclusion ─────────────────────────────────────────────

    def conclude(self, exp_id: str, min_samples: Optional[int] = None) -> Tuple[Optional[Experiment], ExperimentAnalysis]:
        """Wrap up the experiment. Sets winner, status=COMPLETED.

        Promotes the winning variant by writing the promoted config
        to the experiment's `promoted.json` (consumers can read it).

        If the analysis says "tie" or "insufficient_samples", the
        experiment is left RUNNING (so more data can be collected).
        """
        exp = self._cache.get(exp_id)
        if exp is None:
            return None, ExperimentAnalysis(status="not_found")
        analysis = self.analyze(exp_id, min_samples=min_samples)
        if analysis.status != "analyzed" or not analysis.winner or analysis.winner == "tie":
            return exp, analysis
        exp.winner = analysis.winner
        exp.status = ExperimentStatus.COMPLETED.value
        exp.ended_at = datetime.now().isoformat()
        self._save(exp)
        # Promote the winner's config
        winner_variant = exp.variant_by_id(exp.winner)
        if winner_variant is not None:
            self._promote_winner(exp, winner_variant)
        return exp, analysis

    def _promote_winner(self, exp: Experiment, variant: ExperimentVariant) -> None:
        """Persist the winning variant's config to `promoted.json`."""
        exp_dir = self.exp_dir / exp.id
        promoted = {
            "experiment_id": exp.id,
            "target": exp.target,
            "target_key": exp.target_key,
            "winner_variant_id": variant.id,
            "winner_variant_name": variant.name,
            "config": variant.config,
            "promoted_at": datetime.now().isoformat(),
        }
        (exp_dir / "promoted.json").write_text(
            json.dumps(promoted, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Persistence ─────────────────────────────────────────────

    def _save(self, exp: Experiment) -> None:
        exp_dir = self.exp_dir / exp.id
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "experiment.json").write_text(
            json.dumps(exp.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Introspection ───────────────────────────────────────────

    def stats(self) -> dict:
        """Return a snapshot of the manager's state for /status."""
        return {
            "exp_dir": str(self.exp_dir),
            "experiment_count": len(self._cache),
            "running": sum(
                1 for e in self._cache.values()
                if e.status == ExperimentStatus.RUNNING.value
            ),
            "completed": sum(
                1 for e in self._cache.values()
                if e.status == ExperimentStatus.COMPLETED.value
            ),
            "abandoned": sum(
                1 for e in self._cache.values()
                if e.status == ExperimentStatus.ABANDONED.value
            ),
        }


# ── Singleton ──────────────────────────────────────────────────────

_default: Optional[ABTestManager] = None


def get_ab_test_manager() -> ABTestManager:
    """Return the process-wide ABTestManager (lazy-initialized)."""
    global _default
    if _default is None:
        _default = ABTestManager()
    return _default


def reset_ab_test_manager() -> None:
    """Drop the singleton — used in tests."""
    global _default
    _default = None
