"""Session persistence — save/restore conversation state."""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


def _sessions_dir() -> Path:
    p = Path(os.getenv("CODING_AGENT_CACHE_DIR", Path.home() / ".coding-agent")) / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_session(session_id: str, data: dict):
    """Save session to disk."""
    data["updated_at"] = datetime.now().isoformat()
    file_path = _sessions_dir() / f"{session_id}.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_session(session_id: str) -> Optional[dict]:
    """Load a saved session."""
    file_path = _sessions_dir() / f"{session_id}.json"
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text())


def list_sessions() -> list[dict]:
    """List all saved sessions, most recent first."""
    sessions = []
    for f in sorted(_sessions_dir().glob("*.json"), key=os.path.getmtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            data["_file"] = f.stem
            sessions.append(data)
        except Exception:
            pass
    return sessions


def get_latest_session() -> Optional[dict]:
    """Get the most recently saved session."""
    sessions = list_sessions()
    return sessions[0] if sessions else None


def delete_session(session_id: str):
    """Delete a saved session."""
    file_path = _sessions_dir() / f"{session_id}.json"
    if file_path.exists():
        file_path.unlink()


def make_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
