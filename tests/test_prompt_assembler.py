"""Tests for PromptAssembler — structured XML-like sections."""

from agent.prompts.assembler import PromptAssembler
from agent.tools.base import registry


class TestPromptAssembler:
    """Test the structured system prompt builder."""

    def test_base_prompt_contains_identity_and_instructions(self):
        prompt = PromptAssembler.build_system_prompt()
        assert "<identity>" in prompt
        assert "</identity>" in prompt
        assert "<instructions>" in prompt
        assert "</instructions>" in prompt
        assert "AI assistant" in prompt

    def test_install_package_hint_in_prompt(self):
        """Prompt should guide LLM to use install_package for install requests."""
        prompt = PromptAssembler.build_system_prompt()
        assert "install_package" in prompt

    def test_prompt_contains_auto_generated_tool_list(self):
        prompt = PromptAssembler.build_system_prompt()
        assert "<available_tools>" in prompt
        assert "</available_tools>" in prompt
        # Every registered tool's name must appear in the prompt
        for tool in registry.list():
            assert tool.name in prompt, f"Tool '{tool.name}' missing from generated tool list"

    def test_includes_long_term_memory(self):
        prompt = PromptAssembler.build_system_prompt(long_term_memory="User prefers pytest")
        assert "<memory>" in prompt
        assert "</memory>" in prompt
        assert "User prefers pytest" in prompt

    def test_includes_skills(self):
        prompt = PromptAssembler.build_system_prompt(skill_prompt="Skill: setup-flake8")
        assert "<available_skills>" in prompt
        assert "</available_skills>" in prompt
        assert "Skill: setup-flake8" in prompt

    def test_includes_project_context(self):
        prompt = PromptAssembler.build_system_prompt(project_context="Use black for formatting")
        assert "<agent_requestable_workspace_rules>" in prompt
        assert "</agent_requestable_workspace_rules>" in prompt
        assert "Use black for formatting" in prompt

    def test_includes_plan_context(self):
        prompt = PromptAssembler.build_system_prompt(plan_context="Step 1: write tests")
        assert "<plan>" in prompt
        assert "</plan>" in prompt
        assert "Step 1: write tests" in prompt

    def test_includes_spec_context(self):
        prompt = PromptAssembler.build_system_prompt(spec_context="Phase 10: active")
        assert "<spec_context>" in prompt
        assert "</spec_context>" in prompt
        assert "Phase 10: active" in prompt

    def test_includes_failure_context(self):
        prompt = PromptAssembler.build_system_prompt(failure_context="Avoid: mock DB in tests")
        assert "<evolution_notes>" in prompt
        assert "</evolution_notes>" in prompt
        assert "Avoid: mock DB in tests" in prompt

    def test_omits_empty_sections(self):
        prompt = PromptAssembler.build_system_prompt()
        assert "<memory>" not in prompt
        assert "<available_skills>" not in prompt
        assert "<agent_requestable_workspace_rules>" not in prompt
        assert "<plan>" not in prompt
        assert "<spec_context>" not in prompt
        assert "<evolution_notes>" not in prompt

    def test_dead_code_removed(self):
        """EXECUTE_SYSTEM_PROMPT and build_execute_prompt are removed."""
        assert not hasattr(
            PromptAssembler, "EXECUTE_SYSTEM_PROMPT"
        ), "EXECUTE_SYSTEM_PROMPT should be removed (dead code)"
        assert not hasattr(
            PromptAssembler, "build_execute_prompt"
        ), "build_execute_prompt should be removed (dead code)"


class TestPlanPrompt:
    """Test the plan-mode prompt."""

    def test_plan_prompt_has_identity_and_instructions(self):
        prompt = PromptAssembler.build_plan_prompt()
        assert "<identity>" in prompt
        assert "PLANNING mode" in prompt
        assert "MUST NOT write files" in prompt

    def test_plan_prompt_has_tools(self):
        prompt = PromptAssembler.build_plan_prompt()
        assert "<available_tools>" in prompt

    def test_plan_prompt_includes_project_context(self):
        prompt = PromptAssembler.build_plan_prompt(project_context="Use ruff for linting")
        assert "<agent_requestable_workspace_rules>" in prompt
        assert "Use ruff for linting" in prompt

    def test_plan_prompt_includes_memory(self):
        prompt = PromptAssembler.build_plan_prompt(long_term_memory="Prefer async/await")
        assert "<memory>" in prompt
        assert "Prefer async/await" in prompt

    def test_plan_prompt_omits_empty_sections(self):
        prompt = PromptAssembler.build_plan_prompt()
        assert "<memory>" not in prompt
        assert "<agent_requestable_workspace_rules>" not in prompt


class TestSystemReminder:
    """Test the per-turn system-reminder builder."""

    def test_empty_when_no_context(self):
        reminder = PromptAssembler.build_system_reminder()
        assert reminder == ""

    def test_includes_cwd(self):
        reminder = PromptAssembler.build_system_reminder(cwd="/home/user/project")
        assert "<system-reminder>" in reminder
        assert "</system-reminder>" in reminder
        assert "<cwd>/home/user/project</cwd>" in reminder

    def test_includes_mode(self):
        reminder = PromptAssembler.build_system_reminder(mode="auto")
        assert "<mode>auto</mode>" in reminder

    def test_includes_plan_progress(self):
        reminder = PromptAssembler.build_system_reminder(plan_progress="3/5 done")
        assert "<plan_progress>3/5 done</plan_progress>" in reminder

    def test_includes_project_dir(self):
        reminder = PromptAssembler.build_system_reminder(project_dir="my-app")
        assert "<project_dir>my-app</project_dir>" in reminder

    def test_includes_git_status(self):
        reminder = PromptAssembler.build_system_reminder(git_status="On branch feat-x\n M file.py")
        assert "<git_status>" in reminder
        assert "</git_status>" in reminder
        assert "feat-x" in reminder

    def test_combined_context(self):
        reminder = PromptAssembler.build_system_reminder(
            cwd="/work",
            git_status="On branch main",
            plan_progress="2/5 done",
            mode="default",
            project_dir="blog-api",
        )
        assert "<cwd>/work</cwd>" in reminder
        assert "<mode>default</mode>" in reminder
        assert "<plan_progress>2/5 done</plan_progress>" in reminder
        assert "<project_dir>blog-api</project_dir>" in reminder
        assert "On branch main" in reminder

    def test_system_reminder_is_single_block(self):
        """System-reminder should be a single tagged block."""
        reminder = PromptAssembler.build_system_reminder(cwd="/test", mode="plan")
        # Count opening/closing tags — should be exactly one pair
        assert reminder.count("<system-reminder>") == 1
        assert reminder.count("</system-reminder>") == 1


class TestToolListSync:
    """Verify the auto-generated tool list stays in sync with the registry."""

    def test_all_tools_appear_in_prompt(self):
        prompt = PromptAssembler.build_system_prompt()
        registered = {t.name for t in registry.list()}
        for name in registered:
            assert name in prompt, f"Tool '{name}' not found in prompt"

    def test_no_stale_tools_in_prompt(self):
        """Tools listed in <available_tools> must be registered."""
        prompt = PromptAssembler.build_system_prompt()
        registered = {t.name for t in registry.list()}

        # Extract tool names from the <available_tools> section
        import re

        tools_section = prompt.split("<available_tools>")[1].split("</available_tools>")[0]
        listed = set(re.findall(r"- (\w+)\(", tools_section))

        for name in listed:
            assert name in registered, f"Tool '{name}' in prompt but NOT in registry"
