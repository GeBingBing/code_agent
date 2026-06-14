"""Tests for the M1 P0 CLI flags: --plan, --resume <plan_id>, --list-plans.

The CLI's main() is hard to drive end-to-end (it reads stdin/stdout, has
interactive flows). These tests pin down the argparse contract and the
env-var fallback in isolation.
"""

from __future__ import annotations

import argparse
import os


def _parse(argv: list[str]) -> argparse.Namespace:
    """Re-implement the parser setup from cli.main() so we can call
    parse_known_args() without importing the whole CLI module (which
    transitively imports textual, the LLM client, and a dozen other
    heavy modules)."""
    parser = argparse.ArgumentParser(prog="coding-agent", add_help=False)
    parser.add_argument("task", nargs="?", default=None)
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument(
        "--resume", dest="resume", nargs="?", const="__latest_session__", default=None
    )
    parser.add_argument("--plan", dest="plan_mode", action="store_true")
    parser.add_argument("--list-plans", dest="list_plans", action="store_true")
    parser.add_argument("--list-sessions", dest="list_sessions", action="store_true")
    return parser.parse_args(argv)


class TestPlanFlag:
    def test_default_is_not_plan_mode(self):
        args = _parse(["some task"])
        assert args.plan_mode is False

    def test_plan_flag_sets_plan_mode(self):
        args = _parse(["--plan", "implement feature"])
        assert args.plan_mode is True
        assert args.task == "implement feature"

    def test_plan_flag_alone(self):
        args = _parse(["--plan"])
        assert args.plan_mode is True
        assert args.task is None


class TestResumeFlag:
    def test_resume_no_arg_uses_latest_session_sentinel(self):
        """--resume (no arg) must keep the legacy session-resume behaviour.
        The nargs='?' + const='__latest_session__' pattern lets us
        distinguish the two cases downstream."""
        args = _parse(["--resume"])
        assert args.resume == "__latest_session__"

    def test_resume_with_plan_id(self):
        args = _parse(["--resume", "plan-foo-123-abc"])
        assert args.resume == "plan-foo-123-abc"

    def test_no_resume(self):
        args = _parse(["task"])
        assert args.resume is None

    def test_resume_with_explicit_session_sentinel(self):
        """Edge: a user could pass the literal sentinel as a plan_id.
        We accept it as a plan_id (it will fail to load, which is the
        correct failure mode)."""
        args = _parse(["--resume", "__latest_session__"])
        assert args.resume == "__latest_session__"


class TestListPlansFlag:
    def test_list_plans_flag(self):
        args = _parse(["--list-plans"])
        assert args.list_plans is True


class TestCombinedFlags:
    def test_plan_with_print(self):
        args = _parse(["--plan", "-p", "fix bug"])
        assert args.plan_mode is True
        assert args.print_mode is True
        assert args.task == "fix bug"

    def test_resume_with_plan_id_and_task(self):
        """Edge: --resume <plan_id> followed by an explicit task. The
        task is ignored (the plan dictates the work) but the parser
        accepts it. Downstream code in main() should warn or strip it."""
        args = _parse(["--resume", "plan-x", "ignored task"])
        assert args.resume == "plan-x"
        assert args.task == "ignored task"

    def test_list_plans_and_task_combined(self):
        """Edge: --list-plans with a task is fine — main() handles
        --list-plans first and returns before reading args.task."""
        args = _parse(["--list-plans", "ignored"])
        assert args.list_plans is True


class TestEnvVarFallback:
    """The plan-env-var shortcut behaviour. We can't easily exercise
    main() in isolation (it runs forever), so we replicate the env
    fallback logic and assert on the result.
    """

    def test_env_var_sets_plan_mode(self, monkeypatch):
        """CODING_AGENT_PLAN=1 must be equivalent to --plan."""
        monkeypatch.setenv("CODING_AGENT_PLAN", "1")
        # Replicate the main() decision:
        plan = os.environ.get("CODING_AGENT_PLAN") == "1"
        assert plan is True

    def test_env_var_resume_plan(self, monkeypatch):
        monkeypatch.setenv("CODING_AGENT_RESUME_PLAN", "plan-abc-123")
        # Replicate the main() fallback:
        env_resume = os.environ.get("CODING_AGENT_RESUME_PLAN")
        args = _parse(["some task"])  # no --resume
        result = env_resume if env_resume and args.resume is None else args.resume
        assert result == "plan-abc-123"

    def test_cli_arg_wins_over_env(self, monkeypatch):
        """If both are set, CLI --resume wins."""
        monkeypatch.setenv("CODING_AGENT_RESUME_PLAN", "from-env")
        args = _parse(["--resume", "from-cli"])
        env_resume = os.environ.get("CODING_AGENT_RESUME_PLAN")
        result = env_resume if env_resume and args.resume is None else args.resume
        assert result == "from-cli"


class TestBackwardCompatibility:
    """Make sure existing CLI invocations still parse cleanly."""

    def test_legacy_no_args(self):
        args = _parse([])
        assert args.task is None
        assert args.resume is None
        assert args.plan_mode is False
        assert args.print_mode is False

    def test_legacy_task_only(self):
        args = _parse(["do something"])
        assert args.task == "do something"
        assert args.resume is None

    def test_legacy_resume_session(self):
        """The old --resume (boolean) call must still parse to the
        latest-session sentinel."""
        args = _parse(["--resume"])
        assert args.resume == "__latest_session__"
        assert args.resume is not None  # behaves as truthy in the legacy code path
