"""End-to-end routing + execution-chain tests (Phase A).

These exercise the full pipeline from a user input line to which handler
runs, with the LLM and engine replaced by fakes — no real network, no real
tools. They pin the Phase A fixes:

  - A1: an ``agent``-classified task must NOT be silently downgraded to the
    no-tool ``_direct_answer`` path by ``_is_simple_question``.
  - A3: ``_run_edit`` builds its engine with ``max_steps=25``.
  - A4: when the LLM client can't be constructed at startup, the router
    falls back to a legacy-only classifier (not ``None``).
  - A5: the classifier sees the RAW task; the handler sees the
    ``[Files: ...]``-prefixed task.
"""

import asyncio

import pytest

from ui import cli as cli_mod

# ── Shared fakes ────────────────────────────────────────────────


class _FakeEngine:
    """Stand-in for AgentEngine — records config, yields no events."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self.shutdown_called = False

    async def run_stream(self, task):
        # Empty async generator — no events emitted.
        if False:  # pragma: no cover
            yield {}

    async def shutdown(self):
        self.shutdown_called = True


class _NoopSpinner:
    """Stand-in spinner that does nothing and never reports running."""

    is_running = False

    def start(self, *a, **k):
        pass

    async def stop_async(self):
        pass


def _make_cli():
    cli = cli_mod.SimpleCLI()
    # Silence the spinner + echo so capsys stays clean.
    cli._spin = _NoopSpinner()
    return cli


class _FakeRouter:
    """Stand-in router that records the (task, dispatch_task) it sees."""

    def __init__(self, intent="agent"):
        self._intent = intent
        self.route_calls = []

    async def route(self, task, dispatch_task=None):
        self.route_calls.append((task, dispatch_task))
        return "ok"

    def register(self, *a, **k):
        pass


# ── A1: no silent downgrade of agent-classified tasks ──────────


class TestNoSilentDowngrade:
    @pytest.mark.parametrize(
        "task",
        ["how do I run the tests?", "how does async work", "what is asyncio"],
    )
    def test_agent_classified_task_never_calls_direct_answer(self, task, monkeypatch, capsys):
        cli = _make_cli()

        direct_calls = []
        simple_calls = []

        # Force the bug to manifest for EVERY input: even if a task "looks
        # simple", an agent-classified task must keep its tools.
        def fake_simple(t):
            simple_calls.append(t)
            return True

        async def fake_direct(task, engine, start_time):
            direct_calls.append(task)
            return "direct"

        monkeypatch.setattr(cli_mod, "_is_simple_question", fake_simple)
        monkeypatch.setattr(cli, "_direct_answer", fake_direct)
        monkeypatch.setattr(cli, "_echo_user_input", lambda *a, **k: None)
        monkeypatch.setattr("agent.core.engine.AgentEngine", lambda cfg: _FakeEngine(cfg))

        # Fake router always dispatches as "agent".
        cli._router = _FakeRouter("agent")

        asyncio.run(cli._dispatch_user_input(task))

        assert direct_calls == [], f"agent task {task!r} was silently downgraded to _direct_answer"
        assert (
            simple_calls == []
        ), f"_is_simple_question was still consulted for agent task {task!r}"


# ── A3: _run_edit uses max_steps=25 ─────────────────────────────


class TestRunEditStepCap:
    def test_run_edit_uses_max_steps_25(self, monkeypatch, capsys):
        cli = _make_cli()

        captured = {}

        def fake_engine_factory(cfg):
            captured["cfg"] = cfg
            return _FakeEngine(cfg)

        monkeypatch.setattr("agent.core.engine.AgentEngine", fake_engine_factory)
        monkeypatch.setattr(cli, "_echo_user_input", lambda *a, **k: None)

        asyncio.run(cli._run_edit("fix the typo in foo.py"))

        assert (
            captured["cfg"].max_steps == 25
        ), f"_run_edit must cap at max_steps=25, got {captured['cfg'].max_steps}"
        assert captured["cfg"].mode == "auto"


# ── A4: LLM-failure falls back to legacy-only classifier ─────────


class TestRouterStartupFallback:
    def test_setup_router_falls_back_to_legacy_classifier_on_llm_failure(self, monkeypatch):
        cli = cli_mod.SimpleCLI()

        def boom(*a, **k):
            raise RuntimeError("no key")

        monkeypatch.setattr("agent.llm.client.LLMClient", boom)

        cli._setup_router("mock", "mock")

        assert cli._router is not None
        assert (
            cli._router._classifier is not None
        ), "router should get a legacy-only classifier, not None, on LLM failure"
        assert cli._router._classifier.using_llm is False

        # And routing still works (offline legacy → install hermes is 'edit').
        edit_calls = []

        async def edit_h(task):
            edit_calls.append(task)
            return "ok"

        cli._router.register("edit", edit_h)
        asyncio.run(cli._router.route("install hermes"))
        assert edit_calls, "legacy fallback should route 'install hermes' to edit"

    def test_setup_router_rethrows_keyboard_interrupt(self, monkeypatch):
        cli = cli_mod.SimpleCLI()

        def boom(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr("agent.llm.client.LLMClient", boom)

        with pytest.raises(KeyboardInterrupt):
            cli._setup_router("mock", "mock")


# ── A5: classify sees raw task; handler sees context-prefixed ────


class TestDispatchRawTask:
    def test_dispatch_routes_raw_task_to_classifier(self, monkeypatch):
        cli = _make_cli()
        cli.file_context = ["foo.py", "bar.py"]

        fake_router = _FakeRouter()
        cli._router = fake_router
        monkeypatch.setattr(cli, "_echo_user_input", lambda *a, **k: None)

        asyncio.run(cli._dispatch_user_input("how do I run the tests?"))

        assert fake_router.route_calls, "route() was not called"
        task, dispatch_task = fake_router.route_calls[0]
        assert task == "how do I run the tests?", f"classifier got {task!r}"
        assert "[Files:" not in task
        assert dispatch_task.startswith("[Files: foo.py"), f"handler got {dispatch_task!r}"
