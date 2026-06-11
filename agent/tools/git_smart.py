"""Smart Git tools - LLM-powered commit messages, PR creation, branch strategy.

Requires: gh CLI for PR creation (optional)
"""

import asyncio
import re
from typing import Optional

from ..core.workspace import WORKSPACE_ROOT as WORKSPACE
from ..llm.client import LLMClient, Message
from .base import BaseTool, ToolResult, registry


async def _run_git(args: list, cwd: Optional[str] = None, timeout: int = 30) -> tuple:
    """Run a git command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd or str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        proc.returncode,
    )


async def _get_diff(cwd: Optional[str] = None, staged: bool = True) -> str:
    """Get git diff output."""
    args = ["diff", "--cached"] if staged else ["diff"]
    out, _, _ = await _run_git(args, cwd)
    return out


async def _get_diff_stat(cwd: Optional[str] = None) -> str:
    """Get git diff --stat."""
    out, _, _ = await _run_git(["diff", "--stat"], cwd)
    return out


async def _get_recent_commits(cwd: Optional[str] = None, n: int = 5) -> str:
    """Get recent commit messages for style reference."""
    out, _, _ = await _run_git(["log", "--oneline", f"-{n}"], cwd)
    return out


async def _get_changed_files(cwd: Optional[str] = None) -> list:
    """Get list of changed files."""
    out, _, _ = await _run_git(["status", "--porcelain"], cwd)
    files = []
    for line in out.strip().split("\n"):
        if len(line) > 3:
            files.append(line[3:])
    return files


async def _generate_commit_message(
    diff: str, diff_stat: str, recent: str, model: str = None, provider: str = None
) -> str:
    """Use LLM to generate a conventional commit message from diff."""
    # Truncate diff if too long
    max_diff_len = 4000
    if len(diff) > max_diff_len:
        diff = diff[:max_diff_len] + f"\n... ({len(diff) - max_diff_len} more chars)"

    prompt = f"""You are a commit message generator. Analyze the git diff and generate a concise, informative commit message.

Rules:
- Use conventional commits format: type(scope): description
- Types: feat, fix, docs, style, refactor, test, chore
- Keep the subject line under 72 characters
- If there are multiple logical changes, pick the most significant one
- Be specific about WHAT changed, not just "update files"

Recent commit style from this repo:
{recent}

Files changed:
{diff_stat}

Diff:
```diff
{diff}
```

Generate ONLY the commit message subject line (no body, no explanation):"""

    try:
        client = LLMClient(model=model, provider=provider) if model else LLMClient()
        response = await client.chat(
            messages=[Message(role="user", content=prompt)],
            stream=False,
        )
        msg = response if isinstance(response, str) else getattr(response, "content", "")
        # Clean up
        msg = msg.strip().strip('"').strip("'")
        # Remove any prefix like "Commit message:" or "Subject:"
        msg = re.sub(r"^(commit message|subject|message)\s*[:\-]\s*", "", msg, flags=re.I)
        return msg
    except Exception:
        # Fallback to simple message
        return f"update: {diff_stat.splitlines()[0] if diff_stat else 'changes'}"


class SmartCommitTool(BaseTool):
    user_facing_name = "Commit"

    name = "smart_commit"
    description = (
        "Stage changes and create a git commit with an LLM-generated message based on the diff"
    )

    async def execute(self, message: str = "", cwd: Optional[str] = None, **kwargs) -> ToolResult:
        """Smart commit with auto-generated message.

        Args:
            message: Optional override message. If empty, LLM generates one from diff.
            cwd: Working directory
        """
        workspace = cwd or str(WORKSPACE)

        # Check for changes
        files = await _get_changed_files(workspace)
        if not files:
            return ToolResult(success=True, content="Nothing to commit (working tree clean)")

        # Stage all changes
        _, err, rc = await _run_git(["add", "-A"], workspace)
        if rc != 0:
            return ToolResult(success=False, content="", error=f"git add failed: {err}")

        # Generate message if not provided
        if not message:
            diff = await _get_diff(workspace, staged=True)
            stat = await _get_diff_stat(workspace)
            recent = await _get_recent_commits(workspace)
            message = await _generate_commit_message(diff, stat, recent)

        # Commit
        out, err, rc = await _run_git(["commit", "-m", message], workspace)
        if rc != 0:
            return ToolResult(success=False, content=out, error=f"git commit failed: {err}")

        return ToolResult(
            success=True,
            content=f"✓ Committed: {message}\n\nFiles:\n" + "\n".join(f"  {f}" for f in files),
        )

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
                        "message": {
                            "type": "string",
                            "description": "Optional commit message override. If empty, LLM generates from diff.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Working directory for git commands",
                        },
                    },
                },
            },
        }


class CreatePRTool(BaseTool):
    user_facing_name = "PR"

    name = "create_pr"
    description = (
        "Create a GitHub Pull Request using gh CLI with auto-generated title and description"
    )

    async def execute(
        self,
        title: str = "",
        body: str = "",
        base: str = "main",
        draft: bool = False,
        cwd: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """Create a GitHub PR.

        Args:
            title: PR title (auto-generated from branch name + diff if empty)
            body: PR body (auto-generated from diff summary if empty)
            base: Target branch (default: main)
            draft: Create as draft PR
            cwd: Working directory
        """
        workspace = cwd or str(WORKSPACE)

        # Check gh CLI availability
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _, rc = await asyncio.wait_for(proc.communicate(), timeout=5)
            if rc != 0:
                raise FileNotFoundError()
        except (FileNotFoundError, asyncio.TimeoutError):
            return ToolResult(
                success=False,
                content="",
                error="gh CLI not found. Install: https://cli.github.com/",
            )

        # Get current branch
        branch, _, rc = await _run_git(["branch", "--show-current"], workspace)
        branch = branch.strip()
        if rc != 0 or not branch:
            return ToolResult(success=False, content="", error="Not on a git branch")

        if branch == base:
            return ToolResult(
                success=False,
                content="",
                error=f"Cannot create PR from {base} to {base}. Create a feature branch first.",
            )

        # Push branch if not on remote
        await _run_git(["push", "-u", "origin", branch], workspace)

        # Auto-generate title from branch name if not provided
        if not title:
            title = branch.replace("-", " ").replace("_", " ").title()
            if "/" in title:
                title = title.split("/", 1)[-1]

        # Auto-generate body from diff if not provided
        if not body:
            stat = await _get_diff_stat(workspace)
            recent = await _get_recent_commits(workspace, n=3)
            body_lines = ["## Changes", "", "```", stat, "```"]
            if recent:
                body_lines.extend(["", "## Recent commits", "```", recent, "```"])
            body = "\n".join(body_lines)

        # Build gh pr create command
        cmd = ["pr", "create", "--title", title, "--body", body, "--base", base]
        if draft:
            cmd.append("--draft")

        proc = await asyncio.create_subprocess_exec(
            "gh",
            *cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode == 0:
            return ToolResult(
                success=True,
                content=f"✓ PR created: {title}\n{stdout.decode()}",
            )
        else:
            err = stderr.decode()
            # Check if PR already exists
            if "already exists" in err.lower():
                return ToolResult(
                    success=False, content="", error=f"PR already exists for branch {branch}"
                )
            return ToolResult(success=False, content="", error=f"gh pr create failed: {err}")

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
                        "title": {
                            "type": "string",
                            "description": "PR title (auto-generated if empty)",
                        },
                        "body": {
                            "type": "string",
                            "description": "PR body (auto-generated if empty)",
                        },
                        "base": {
                            "type": "string",
                            "default": "main",
                            "description": "Target branch",
                        },
                        "draft": {
                            "type": "boolean",
                            "default": False,
                            "description": "Create as draft PR",
                        },
                    },
                },
            },
        }


class SmartBranchTool(BaseTool):
    user_facing_name = "Branch"

    name = "smart_branch"
    description = "Create a feature branch with an auto-generated name from task description, or switch to an existing branch"

    async def execute(
        self,
        task: str = "",
        branch: str = "",
        switch: bool = False,
        cwd: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """Smart branch management.

        Args:
            task: Task description to generate branch name from (e.g., 'fix auth bug')
            branch: Explicit branch name (overrides task-based generation)
            switch: If True and branch exists, switch to it instead of error
            cwd: Working directory
        """
        workspace = cwd or str(WORKSPACE)

        # Get current branch
        current, _, rc = await _run_git(["branch", "--show-current"], workspace)
        if rc != 0:
            return ToolResult(success=False, content="", error="Not a git repository")

        # Generate or use explicit branch name
        if branch:
            new_branch = branch
        elif task:
            new_branch = _generate_branch_name(task)
        else:
            return ToolResult(
                success=False,
                content="",
                error="Provide either 'task' (to auto-generate name) or 'branch' (explicit name)",
            )

        # Check if branch exists
        out, _, _ = await _run_git(["branch", "--list", new_branch], workspace)
        if out.strip():
            if switch:
                _, err, rc = await _run_git(["checkout", new_branch], workspace)
                if rc == 0:
                    return ToolResult(
                        success=True, content=f"✓ Switched to existing branch: {new_branch}"
                    )
                return ToolResult(success=False, content="", error=f"Checkout failed: {err}")
            return ToolResult(
                success=False,
                content="",
                error=f"Branch '{new_branch}' already exists. Use switch=true to switch to it.",
            )

        # Create new branch from current
        _, err, rc = await _run_git(["checkout", "-b", new_branch], workspace)
        if rc != 0:
            return ToolResult(success=False, content="", error=f"Failed to create branch: {err}")

        return ToolResult(
            success=True,
            content=f"✓ Created and switched to branch: {new_branch}\n(previous: {current.strip()})",
        )

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
                        "task": {
                            "type": "string",
                            "description": "Task description to generate branch name from",
                        },
                        "branch": {
                            "type": "string",
                            "description": "Explicit branch name (overrides task)",
                        },
                        "switch": {
                            "type": "boolean",
                            "default": False,
                            "description": "Switch to branch if it already exists",
                        },
                    },
                },
            },
        }


def _generate_branch_name(task: str) -> str:
    """Generate a kebab-case branch name from task description."""
    import re

    # Extract keywords
    words = re.findall(r"[a-zA-Z]+", task.lower())
    stopwords = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "to",
        "and",
        "or",
        "in",
        "on",
        "at",
        "for",
        "with",
        "of",
        "from",
        "by",
        "as",
        "it",
        "this",
        "that",
        "add",
        "fix",
        "update",
        "implement",
        "create",
        "delete",
        "remove",
        "refactor",
        "test",
        "docs",
    }
    keywords = [w for w in words if w not in stopwords and len(w) > 2]

    # Detect type prefix
    prefix = "feat"
    task_lower = task.lower()
    if any(w in task_lower for w in ("fix", "bug", "error", "crash", "broken", "repair")):
        prefix = "fix"
    elif any(w in task_lower for w in ("test", "spec", "unit test", "integration test")):
        prefix = "test"
    elif any(w in task_lower for w in ("doc", "readme", "comment", "guide")):
        prefix = "docs"
    elif any(w in task_lower for w in ("refactor", "clean", "restructure", "rewrite")):
        prefix = "refactor"
    elif any(
        w in task_lower for w in ("chore", "ci", "build", "lint", "format", "deps", "dependency")
    ):
        prefix = "chore"

    # Build name: prefix/keyword1-keyword2
    if keywords:
        name = "-".join(keywords[:4])
    else:
        name = "update"

    return f"{prefix}/{name}"


# Register tools
registry.register(SmartCommitTool())
registry.register(CreatePRTool())
registry.register(SmartBranchTool())
