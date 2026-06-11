"""Skill manager - save, retrieve, and activate reusable skills"""

import re
from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass

import yaml

from .base import BaseTool, ToolResult, registry


SKILLS_DIR = Path.home() / ".coding-agent" / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Skill:
    name: str
    description: str
    tags: List[str]
    content: str
    source_file: Path

    def to_prompt(self) -> str:
        """Format skill as a prompt injection."""
        return f"""## Skill: {self.name}
{self.description}
Tags: {', '.join(self.tags)}

{self.content}
"""


class SkillManager:
    """Manage reusable skills stored as Markdown + YAML files."""

    def __init__(self, skills_dir: Optional[Path] = None):
        self.skills_dir = skills_dir or SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> Skill:
        """Create and save a new skill file."""
        tags = tags or []
        frontmatter = {
            "name": name,
            "description": description,
            "tags": tags,
        }
        yaml_text = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
        file_text = f"---\n{yaml_text}---\n\n{content}\n"

        filepath = self.skills_dir / f"{name}.md"
        filepath.write_text(file_text, encoding="utf-8")

        return Skill(
            name=name,
            description=description,
            tags=tags,
            content=content,
            source_file=filepath,
        )

    def list_skills(self) -> List[Skill]:
        """List all available skills."""
        skills = []
        for filepath in sorted(self.skills_dir.glob("*.md")):
            skill = self._parse_skill_file(filepath)
            if skill:
                skills.append(skill)
        return skills

    def search_skills(self, query: str) -> List[Skill]:
        """Search skills by name, description, or tags (substring match)."""
        query = query.lower()
        results = []
        for skill in self.list_skills():
            if (
                query in skill.name.lower()
                or query in skill.description.lower()
                or any(query in tag.lower() for tag in skill.tags)
            ):
                results.append(skill)
        return results

    # ── Semantic search (vector-based) ────────────────────────────

    def _embed_text(self, text: str) -> dict:
        """Build a simple TF-IDF-style sparse vector: word → frequency."""
        import re
        text = text.lower()
        # Split on non-alphanumeric, keep CJK unigrams
        tokens = []
        for tok in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text):
            tokens.append(tok)
        vec = {}
        for t in tokens:
            vec[t] = vec.get(t, 0) + 1
        # Normalize by length
        n = max(1, sum(vec.values()))
        return {k: v / n for k, v in vec.items()}

    def _cosine(self, a: dict, b: dict) -> float:
        """Cosine similarity between two sparse dict-vectors."""
        common = set(a.keys()) & set(b.keys())
        if not common:
            return 0.0
        dot = sum(a[k] * b[k] for k in common)
        norm_a = sum(v * v for v in a.values()) ** 0.5
        norm_b = sum(v * v for v in b.values()) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def semantic_search(self, query: str, top_k: int = 3) -> List[tuple[Skill, float]]:
        """Vector similarity search — finds semantically related skills.

        Embeds each skill's (name + description + tags + content) and the query,
        then ranks by cosine similarity. Falls back to substring search if no
        semantic match.
        """
        query_vec = self._embed_text(query)
        scored: list[tuple[Skill, float]] = []
        for skill in self.list_skills():
            # Weight the title higher than content
            title = f"{skill.name} {' '.join(skill.tags)}"
            desc = skill.description
            full = f"{title} {desc} {skill.content}"
            skill_vec = self._embed_text(full)
            score = self._cosine(query_vec, skill_vec)
            if score > 0.05:  # threshold
                scored.append((skill, score))

        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def activate_skills_semantic(self, query: str, top_k: int = 2) -> str:
        """Semantic variant of activate_skills — uses vector similarity."""
        results = self.semantic_search(query, top_k)
        if not results:
            return self.activate_skills(query)  # fallback
        return "\n".join(s.to_prompt() for s, score in results)

    def load_skill(self, name: str) -> Optional[Skill]:
        """Load a skill by name."""
        filepath = self.skills_dir / f"{name}.md"
        if not filepath.exists():
            return None
        return self._parse_skill_file(filepath)

    def activate_skills(self, query: str) -> str:
        """Search and return skill prompts for injection."""
        skills = self.search_skills(query)
        if not skills:
            return ""
        return "\n".join(s.to_prompt() for s in skills)

    def _parse_skill_file(self, filepath: Path) -> Optional[Skill]:
        """Parse a Markdown + YAML skill file."""
        text = filepath.read_text(encoding="utf-8")

        # Match YAML frontmatter
        match = re.match(r"^---\n(.*?)\n---\n\n?(.*)$", text, re.DOTALL)
        if not match:
            return None

        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None

        return Skill(
            name=frontmatter.get("name", filepath.stem),
            description=frontmatter.get("description", ""),
            tags=frontmatter.get("tags", []),
            content=match.group(2).strip(),
            source_file=filepath,
        )


# === Tools for the agent ===

class CreateSkillTool(BaseTool):
    user_facing_name = "Skill"

    name = "create_skill"
    description = "Save a reusable skill for future tasks"

    def __init__(self):
        self.manager = SkillManager()

    async def execute(self, name: str, description: str, content: str, tags: Optional[List[str]] = None, **kwargs) -> ToolResult:
        try:
            skill = self.manager.create_skill(name, description, content, tags)
            return ToolResult(success=True, content=f"Skill saved: {skill.source_file}")
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill identifier (snake_case)"},
                        "description": {"type": "string", "description": "What this skill does"},
                        "content": {"type": "string", "description": "The skill instructions/content"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Search tags"},
                    },
                    "required": ["name", "description", "content"],
                },
            },
        }


class ListSkillsTool(BaseTool):
    user_facing_name = "List"

    is_concurrency_safe = True
    is_read_only = True
    name = "list_skills"
    description = "List all saved skills"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def __init__(self):
        self.manager = SkillManager()

    async def execute(self, **kwargs) -> ToolResult:
        try:
            skills = self.manager.list_skills()
            if not skills:
                return ToolResult(success=True, content="No skills saved yet.")
            lines = [f"- {s.name}: {s.description} (tags: {', '.join(s.tags)})" for s in skills]
            return ToolResult(success=True, content="\n".join(lines))
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))


class SearchSkillsTool(BaseTool):
    user_facing_name = "Search"

    is_concurrency_safe = True
    is_read_only = True
    name = "search_skills"
    description = "Search saved skills by keyword"

    def __init__(self):
        self.manager = SkillManager()

    async def execute(self, query: str, **kwargs) -> ToolResult:
        try:
            skills = self.manager.search_skills(query)
            if not skills:
                return ToolResult(success=True, content=f"No skills found for '{query}'.")
            lines = [f"- {s.name}: {s.description}" for s in skills]
            return ToolResult(success=True, content="\n".join(lines))
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keyword"},
                    },
                    "required": ["query"],
                },
            },
        }


# Register tools
registry.register(CreateSkillTool())
registry.register(ListSkillsTool())
registry.register(SearchSkillsTool())
