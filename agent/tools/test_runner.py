"""RunTests tool — execute pytest and return structured results."""

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .base import BaseTool, ToolResult, registry


class RunTestsTool(BaseTool):
    user_facing_name = "Test"

    name = "run_tests"
    description = "Run project tests with pytest and return pass/fail results with failure details"

    async def execute(
        self,
        path: str = "tests/",
        marker: str = "",
        verbose: bool = False,
        **kwargs,
    ) -> ToolResult:
        """Run pytest tests.

        Args:
            path: Test file or directory to run (default: tests/)
            marker: Pytest marker filter, e.g. "unit" or "slow"
            verbose: Show full pytest output instead of summary
        """
        cmd = [sys.executable, "-m", "pytest", path, "-q"]

        if marker:
            cmd.extend(["-m", marker])
        if verbose:
            cmd.append("-v")
        if not verbose:
            cmd.append("--tb=short")

        # Add --no-header to suppress pytest header
        cmd.append("--no-header")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONWARNINGS": "ignore"},
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return ToolResult(
                    success=False, content="", error="Tests timed out after 120s"
                )

            output = stdout.decode("utf-8", errors="replace")
            error_output = stderr.decode("utf-8", errors="replace")

            # Parse pytest output for pass/fail counts
            passed = 0
            failed = 0
            errors = 0

            # Look for the summary line: "X passed, Y failed, Z errors"
            summary_match = re.search(
                r"(\d+)\s+passed.*?(\d+)\s+failed.*?(\d+)\s+error",
                output,
                re.IGNORECASE,
            )
            if summary_match:
                passed = int(summary_match.group(1))
                failed = int(summary_match.group(2))
                errors = int(summary_match.group(3))
            else:
                # Try simpler: "X passed"
                simple_pass = re.search(r"(\d+)\s+passed", output)
                if simple_pass:
                    passed = int(simple_pass.group(1))
                simple_fail = re.search(r"(\d+)\s+failed", output)
                if simple_fail:
                    failed = int(simple_fail.group(1))

            # Extract failure details (last N lines before summary)
            failure_details = []
            in_failure = False
            for line in output.split("\n"):
                if line.startswith("FAILED") or line.startswith("ERROR"):
                    in_failure = True
                if in_failure:
                    failure_details.append(line)
                if "short test summary" in line.lower():
                    in_failure = True

            failures_text = "\n".join(failure_details) if failure_details else ""

            # Build result
            total = passed + failed + errors
            if proc.returncode == 0 and failed == 0 and errors == 0:
                result_lines = [
                    f"Tests: {passed} passed in {self._extract_time(output)}",
                    "",
                    "All tests passed.",
                ]
                return ToolResult(success=True, content="\n".join(result_lines))

            result_lines = [
                f"Tests: {passed} passed, {failed} failed, {errors} errors in {self._extract_time(output)}",
                "",
            ]

            if failures_text:
                result_lines.append("Failures:")
                result_lines.append(failures_text[:2000])  # Truncate for context window
                result_lines.append(
                    "Hint: Review the failures above. Check the corresponding source files and fix the issues."
                )

            return ToolResult(
                success=False,
                content="\n".join(result_lines),
                error=f"{failed} failed, {errors} errors" if (failed + errors) > 0 else None,
            )

        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))

    def _extract_time(self, output: str) -> str:
        """Extract execution time from pytest output."""
        match = re.search(r"in\s+([\d.]+s)", output)
        return match.group(1) if match else "?"

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
                        "path": {
                            "type": "string",
                            "description": "Test file or directory to run (default: tests/)",
                            "default": "tests/",
                        },
                        "marker": {
                            "type": "string",
                            "description": "Pytest marker filter, e.g. 'unit' or 'slow'",
                        },
                        "verbose": {
                            "type": "boolean",
                            "description": "Show full pytest output instead of summary",
                            "default": False,
                        },
                    },
                    "required": [],
                },
            },
        }


# Register tool
registry.register(RunTestsTool())


class WriteFailingTestTool(BaseTool):
    """TDD RED step — write a test that is EXPECTED to fail.

    Use this to start a TDD cycle. Writes the test file, runs it, and
    confirms the failure is recorded in the TDD state machine.
    """

    name = "write_failing_test"
    description = (
        "TDD RED step: write a test that is EXPECTED to fail. "
        "Writes the test file, runs pytest, and reports the failure. "
        "Use this to start a TDD cycle before implementing new code."
    )

    async def execute(
        self,
        path: str,
        test_code: str,
        feature: str = "",
        **kwargs,
    ) -> ToolResult:
        """Write a test file and confirm it fails.

        Args:
            path: Path to test file (e.g. "tests/test_foo.py")
            test_code: Python source code of the test
            feature: Short feature name for the TDD cycle (optional)
        """
        from pathlib import Path as _Path
        from .base import registry as _registry

        target = _Path(path)
        if not target.is_absolute():
            from ..core.workspace import WORKSPACE_ROOT as WORKSPACE
            target = WORKSPACE / path

        # Write the test
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(test_code, encoding="utf-8")
        except Exception as e:
            return ToolResult(
                success=False, content="",
                error=f"Failed to write test file: {e}"
            )

        # Run the test — expect it to fail
        run_tool = _registry.get("run_tests")
        if not run_tool:
            return ToolResult(
                success=False, content="",
                error="run_tests tool not registered"
            )

        run_result = await run_tool.execute(path=str(target), verbose=False)

        # The RED step is satisfied if:
        #   - The test file was written
        #   - pytest ran (even if it failed)
        #   - We got a structured result back
        if not run_result.success:
            # This is actually the EXPECTED outcome for RED
            content = (
                f"[TDD RED step] Wrote test to {path}\n"
                f"Test failed as expected — RED step complete.\n\n"
                f"{run_result.content or run_result.error or ''}\n\n"
                f"Next: write implementation to make this test pass (GREEN step)."
            )
            return ToolResult(
                success=True,
                content=content,
                metadata={"tdd_step": "red", "test_path": path, "feature": feature},
            )

        # Test unexpectedly passed — this is a hint, not a hard fail
        content = (
            f"[TDD RED step] Wrote test to {path}\n"
            f"WARNING: Test PASSED on first run. Either the feature already exists, "
            f"or the test doesn't actually test the new behavior. "
            f"Consider writing a stricter test before proceeding to GREEN.\n\n"
            f"{run_result.content}"
        )
        return ToolResult(
            success=True,
            content=content,
            metadata={"tdd_step": "red", "test_path": path, "feature": feature, "unexpected_pass": True},
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
                        "path": {
                            "type": "string",
                            "description": "Path to test file (e.g. 'tests/test_foo.py')",
                        },
                        "test_code": {
                            "type": "string",
                            "description": "Python source code of the test",
                        },
                        "feature": {
                            "type": "string",
                            "description": "Short feature name for the TDD cycle (optional)",
                        },
                    },
                    "required": ["path", "test_code"],
                },
            },
        }


# Register tool
registry.register(WriteFailingTestTool())
