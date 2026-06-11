"""User profile — persistent identity for the human user.

Stored at ~/.coding-agent/user_profile.json, separate from memory.md so:
  1. memory.md's 50-entry LRU cap cannot evict identity
  2. memory.md's brittle line format (key: value\\n) can't be corrupted
     by multi-line values
  3. Identity has a stable schema for the agent to rely on

This is the root-cause fix for "session forgetting" — the agent's
long-term memory layer (memory.md) was never written to for user facts
in the first place, AND the 50-entry cap would evict them anyway.
The user profile is a separate, never-evicted file with a clear schema.

Reading:  UserProfile.load() — called at engine __init__ and at every
          CLI/TUI session start
Writing:  UserProfile.remember_fact() — called by /remember command,
          /memory add, and the auto-extractor (PR-14 fact_extractor)
          UserProfile.forget() — called by /forget command
Injection: UserProfile.to_prompt() — rendered as <user_profile> XML
          and injected into system prompt before <memory>
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

# Default path — overridable via env CODING_AGENT_USER_PROFILE
_DEFAULT_PATH = Path("~/.coding-agent/user_profile.json").expanduser()

# ── L2 schema check constants ──────────────────────────────────────
# Reuse NAME_BLACKLIST from fact_extractor — single source of truth.
from .fact_extractor import _QUESTION_END_MARKERS, NAME_BLACKLIST  # noqa: E402

# Per-key max length (chars). Strict defaults; anything longer is rejected
# by the L2 schema check. These are upper bounds, not recommendations.
_VALUE_MAX_LEN = {
    "name": 20,
    "preferred_name": 20,
    "pronouns": 15,
    "language": 20,
    "timezone": 30,
    "expertise_level": 20,
}
_VALUE_MAX_LEN_DEFAULT = 200  # For important_facts entries and prefs

# Punctuation that's never legitimate inside a name/handle. The
# extractor should never have produced a value containing these.
_NAME_FORBIDDEN_CHARS = ("?", "？", "!", "！", "*", "#", "@")


def _validate_value(key: str, value: str) -> None:
    """L2 schema check. Raise ValueError if value is implausible for `key`.

    Strict: empty / over-cap / banned-punctuation / blacklist-word values
    are rejected. The caller (UserProfile.remember_fact) catches the
    exception and skips the write, so a bad extractor hit never corrupts
    the profile. No silent fallback — the bad value is dropped.
    """
    if not value or not value.strip():
        raise ValueError("empty value")
    s = value.strip()
    cap = _VALUE_MAX_LEN.get(key, _VALUE_MAX_LEN_DEFAULT)
    if len(s) > cap:
        raise ValueError(f"value too long for {key}: {len(s)} > {cap}")
    if key in ("name", "preferred_name"):
        if s[-1] in _QUESTION_END_MARKERS:
            raise ValueError(f"name ends with question marker: {s!r}")
        if any(c in s for c in _NAME_FORBIDDEN_CHARS):
            raise ValueError(f"name contains forbidden char: {s!r}")
        if s.lower() in NAME_BLACKLIST:
            raise ValueError(f"name is in blacklist: {s!r}")


# ── L4 change log ──────────────────────────────────────────────────
_CHANGE_LOG_MAX = 100  # Cap to avoid unbounded growth across long sessions


@dataclass
class ChangeRecord:
    """One entry in the per-field undo log (L4).

    Captures `before`/`after` so undo_last() can revert. `source`
    distinguishes extractor writes from manual /remember calls.
    """

    timestamp: str
    action: str  # "remember_fact" | "remember_preference" | "forget" | "clear"
    key: str  # Field name (e.g. "name") or fact/pref key
    before: Optional[str]
    after: Optional[str]
    source: str  # "extractor" | "command" | "manual"


def _default_path() -> Path:
    """Resolve user profile path, honoring env override."""
    env = os.environ.get("CODING_AGENT_USER_PROFILE", "").strip()
    if env:
        return Path(env).expanduser()
    return _DEFAULT_PATH


@dataclass
class UserProfile:
    """A persistent, never-evicted identity for the human user.

    Designed to be loaded once per session and updated sparsely (only
    when the user says something new about themselves).
    """

    # ── Identity fields (frequently surfaced) ──
    name: Optional[str] = None
    preferred_name: Optional[str] = None
    pronouns: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    expertise_level: Optional[str] = None

    # ── Free-form facts (rarely surfaced but never lost) ──
    important_facts: list[str] = field(default_factory=list)
    preferences: dict[str, str] = field(default_factory=dict)
    custom_instructions: str = ""

    # ── Metadata ──
    updated_at: str = ""
    created_at: str = ""

    # ── L4 audit (capped at _CHANGE_LOG_MAX entries) ──
    change_log: List[ChangeRecord] = field(default_factory=list)

    # ── Aliases for known fields (PR-14) ──
    FIELD_ALIASES = {
        "name": "name",
        "user_name": "name",
        "username": "name",
        "preferred_name": "preferred_name",
        "nickname": "preferred_name",
        "nick": "preferred_name",
        "pronouns": "pronouns",
        "pronoun": "pronouns",
        "language": "language",
        "lang": "language",
        "locale": "language",
        "timezone": "timezone",
        "tz": "timezone",
        "expertise": "expertise_level",
        "expertise_level": "expertise_level",
        "level": "expertise_level",
        "skill_level": "expertise_level",
    }

    # ── I/O ──

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "UserProfile":
        """Load from disk. Returns empty profile on any error."""
        p = path or _default_path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            return cls.from_dict(data)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # Corrupted file — return empty profile, don't crash
            return cls()

    def save(self, path: Optional[Path] = None) -> None:
        """Atomic write: serialize to tmp, then rename. Survives mid-write crashes."""
        p = path or _default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not self.created_at:
            self.created_at = self.updated_at

        payload = json.dumps(asdict(self), indent=2, ensure_ascii=False)
        # Atomic write via tmp file in the same directory
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".user_profile.",
                suffix=".json.tmp",
                dir=str(p.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, p)
            except Exception:
                # Clean up tmp on failure
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except OSError:
            # Fallback to non-atomic if rename fails (e.g., cross-device)
            p.write_text(payload, encoding="utf-8")

    # ── Mutation API ──

    def _log_change(
        self,
        key: str,
        before: Optional[str],
        after: Optional[str],
        action: str,
        source: str = "extractor",
    ) -> None:
        """L4: append a ChangeRecord and trim the log to its cap.

        Called by remember_fact / remember_preference / forget / clear
        just before save(). Keeps at most _CHANGE_LOG_MAX entries — the
        most recent — to bound disk usage.
        """
        self.change_log.append(
            ChangeRecord(
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                action=action,
                key=key,
                before=before,
                after=after,
                source=source,
            )
        )
        if len(self.change_log) > _CHANGE_LOG_MAX:
            # Keep only the most recent _CHANGE_LOG_MAX entries.
            self.change_log = self.change_log[-_CHANGE_LOG_MAX:]

    def remember_fact(self, key: str, value: str, source: str = "extractor") -> None:
        """Update a known field or append to important_facts.

        Examples:
            remember_fact("name", "hay")           → sets self.name = "hay"
            remember_fact("user.name", "hay")      → same as above
            remember_fact("nickname", "H")         → sets self.preferred_name
            remember_fact("favorite_editor", "vim") → appends to important_facts
        """
        if not key or value is None:
            return
        # Strip "user." prefix and normalize
        norm_key = key.lower().strip()
        if norm_key.startswith("user."):
            norm_key = norm_key[5:]

        attr = self.FIELD_ALIASES.get(norm_key)
        target_value = str(value).strip() or None

        # ── L2: schema check BEFORE write ──
        # Rejects implausible values that slipped past L0 (regex guard)
        # and L1 (LLM prompt加固). Logs the rejection to the global
        # AuditLogger so operators can see what was dropped.
        if target_value is not None and attr in _VALUE_MAX_LEN:
            try:
                _validate_value(attr, target_value)
            except ValueError as exc:
                try:
                    from .audit_log import get_audit_logger

                    get_audit_logger().log(
                        {
                            "action": "profile_reject",
                            "key": attr,
                            "value": target_value,
                            "reason": "schema_check_failed",
                            "error": str(exc),
                        }
                    )
                except Exception:
                    pass  # Audit log failure must never break the write path
                return  # Reject — no write, no change_log entry.

        # ── existing write logic (extended with L4 audit) ──
        if attr:
            prev = getattr(self, attr, None)
            setattr(self, attr, target_value)
            # L4: record only if the value actually changed
            if target_value != prev:
                self._log_change(attr, prev, target_value, "remember_fact", source)
        else:
            entry = f"{norm_key}: {target_value}"
            if entry not in self.important_facts:
                self.important_facts.append(entry)
                self._log_change("important_facts", None, entry, "remember_fact", source)
        self.save()

    def remember_preference(self, key: str, value: str, source: str = "extractor") -> None:
        """Set a preference (separate from identity facts)."""
        if not key:
            return
        norm_key = key.lower().strip()
        prev = self.preferences.get(norm_key)
        self.preferences[norm_key] = str(value).strip()
        # L4: log if value actually changed
        if prev != self.preferences[norm_key]:
            self._log_change(
                norm_key, prev, self.preferences[norm_key], "remember_preference", source
            )
        self.save()

    def forget(self, key: str, source: str = "command") -> bool:
        """Remove a field, fact, or preference. Returns True if anything was removed.

        Tries in this order:
          1. Known field (name/pronouns/etc.)
          2. Important fact (by key prefix)
          3. Preference (exact key match)
        """
        if not key:
            return False
        norm_key = key.lower().strip()
        if norm_key.startswith("user."):
            norm_key = norm_key[5:]

        # 1. Try known fields
        attr = self.FIELD_ALIASES.get(norm_key)
        if attr and attr in (
            "name",
            "preferred_name",
            "pronouns",
            "language",
            "timezone",
            "expertise_level",
        ):
            prev = getattr(self, attr, None)
            if prev is not None:
                setattr(self, attr, None)
                # L4: record before/after so undo can restore
                self._log_change(attr, prev, None, "forget", source)
                self.save()
                return True

        # 2. Try important_facts
        prefix = f"{norm_key}:"
        for i, fact in enumerate(self.important_facts):
            if fact.lower().startswith(prefix):
                self.important_facts.pop(i)
                # L4: record the removed fact
                self._log_change("important_facts", fact, None, "forget", source)
                self.save()
                return True

        # 3. Try preferences (exact match)
        if norm_key in self.preferences:
            prev = self.preferences.pop(norm_key)
            # L4: record the removed preference
            self._log_change(norm_key, prev, None, "forget", source)
            self.save()
            return True

        return False

    def clear(self) -> None:
        """Wipe all fields. Used by /profile clear or for testing."""
        # L4: snapshot before clearing so undo can restore everything
        snapshot = {
            "name": self.name,
            "preferred_name": self.preferred_name,
            "pronouns": self.pronouns,
            "language": self.language,
            "timezone": self.timezone,
            "expertise_level": self.expertise_level,
            "important_facts": list(self.important_facts),
            "preferences": dict(self.preferences),
        }
        self.name = None
        self.preferred_name = None
        self.pronouns = None
        self.language = None
        self.timezone = None
        self.expertise_level = None
        self.important_facts = []
        self.preferences = {}
        self.custom_instructions = ""
        # L4: record a single "clear" entry (action; before is opaque)
        self._log_change("__all__", str(snapshot), None, "clear", "command")
        self.save()

    def undo_last(self) -> Optional[ChangeRecord]:
        """Revert the most recent change. Returns the reverted record or None.

        Walks the change_log in reverse:
          - "remember_fact" / "remember_preference" → restore the `before` value
          - "forget" → re-set the value to `before`
          - "clear" → restore the snapshot of all fields
        Pops the record and saves. The most recent change only; for
        deeper undo, call this repeatedly.
        """
        if not self.change_log:
            return None
        record = self.change_log[-1]
        try:
            if record.action == "clear":
                # Reconstruct from the opaque before-snapshot
                import ast

                try:
                    snapshot = ast.literal_eval(record.before) if record.before else {}
                except (ValueError, SyntaxError):
                    snapshot = {}
                self.name = snapshot.get("name")
                self.preferred_name = snapshot.get("preferred_name")
                self.pronouns = snapshot.get("pronouns")
                self.language = snapshot.get("language")
                self.timezone = snapshot.get("timezone")
                self.expertise_level = snapshot.get("expertise_level")
                self.important_facts = list(snapshot.get("important_facts", []))
                self.preferences = dict(snapshot.get("preferences", {}))
            elif record.action in ("remember_fact", "remember_preference"):
                # Revert the value: set the field back to `before`
                if record.key == "important_facts":
                    # The recorded "after" was the full "key: value" entry.
                    # Remove it from the list (idempotent if already gone).
                    after = record.after
                    if after and after in self.important_facts:
                        self.important_facts.remove(after)
                else:
                    # Standard field or preference
                    attr = self.FIELD_ALIASES.get(record.key, record.key)
                    if hasattr(self, attr):
                        setattr(self, attr, record.before)
                    elif record.key in self.preferences:
                        # Preference key (no FIELD_ALIASES hit)
                        if record.before is None:
                            self.preferences.pop(record.key, None)
                        else:
                            self.preferences[record.key] = record.before
            elif record.action == "forget":
                # Re-set the value to `before` (the value that was removed)
                attr = self.FIELD_ALIASES.get(record.key, record.key)
                if hasattr(self, attr):
                    setattr(self, attr, record.before)
                elif record.key in self.preferences:
                    if record.before is not None:
                        self.preferences[record.key] = record.before
                elif record.key == "important_facts" and record.before:
                    if record.before not in self.important_facts:
                        self.important_facts.append(record.before)
        finally:
            # Always pop the record, even if revert was a no-op
            self.change_log.pop()
            self.save()
        return record

    # ── Inspection ──

    def is_empty(self) -> bool:
        return not any(
            [
                self.name,
                self.preferred_name,
                self.pronouns,
                self.language,
                self.timezone,
                self.expertise_level,
                self.preferences,
                self.important_facts,
                self.custom_instructions,
            ]
        )

    def summary(self) -> str:
        """One-line human-readable summary. For /whoami quick view."""
        if self.is_empty():
            return "(no profile data yet)"
        parts = []
        if self.name:
            parts.append(f"name={self.name}")
        if self.pronouns:
            parts.append(f"pronouns={self.pronouns}")
        if self.language:
            parts.append(f"lang={self.language}")
        if self.timezone:
            parts.append(f"tz={self.timezone}")
        if self.expertise_level:
            parts.append(f"level={self.expertise_level}")
        n_prefs = len(self.preferences)
        n_facts = len(self.important_facts)
        if n_prefs:
            parts.append(f"{n_prefs} preferences")
        if n_facts:
            parts.append(f"{n_facts} facts")
        return ", ".join(parts)

    def to_prompt(self, max_facts: int = 10) -> str:
        """Render as <user_profile> XML for system prompt injection.

        Returns empty string if profile is empty.
        """
        if self.is_empty():
            return ""
        lines = ["<user_profile>"]
        if self.name:
            lines.append(f"  Name: {self.name}")
        if self.preferred_name and self.preferred_name != self.name:
            lines.append(f"  Preferred name: {self.preferred_name}")
        if self.pronouns:
            lines.append(f"  Pronouns: {self.pronouns}")
        if self.language:
            lines.append(f"  Language: {self.language}")
        if self.timezone:
            lines.append(f"  Timezone: {self.timezone}")
        if self.expertise_level:
            lines.append(f"  Expertise: {self.expertise_level}")
        if self.preferences:
            lines.append("  Preferences:")
            for k, v in self.preferences.items():
                # Truncate long values to bound size
                v_display = v if len(v) <= 100 else v[:97] + "..."
                lines.append(f"    - {k}: {v_display}")
        if self.important_facts:
            lines.append("  Known facts:")
            # Most recent last (or all if fewer than max)
            facts_to_show = self.important_facts[-max_facts:]
            for fact in facts_to_show:
                fact_display = fact if len(fact) <= 200 else fact[:197] + "..."
                lines.append(f"    - {fact_display}")
        if self.custom_instructions:
            ci = (
                self.custom_instructions
                if len(self.custom_instructions) <= 500
                else self.custom_instructions[:497] + "..."
            )
            lines.append(f"  Custom instructions: {ci}")
        lines.append("</user_profile>")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return as dict (for JSON serialization and testing)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        """Build from dict (for testing)."""
        if not isinstance(data, dict):
            return cls()
        valid = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in valid}
        # L4: deserialize change_log dicts back to ChangeRecord instances
        if "change_log" in filtered and isinstance(filtered["change_log"], list):
            filtered["change_log"] = [
                ChangeRecord(**rec) if isinstance(rec, dict) else rec
                for rec in filtered["change_log"]
            ]
        return cls(**filtered)


def reset_user_profile_for_tests(path: Optional[Path] = None) -> None:
    """Delete the profile file (testing only)."""
    p = path or _default_path()
    if p.exists():
        p.unlink()
