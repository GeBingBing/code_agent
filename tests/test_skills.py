"""Tests for Phase 2: Skill manager functional tests."""

import asyncio

from agent.tools.skill_manager import (
    CreateSkillTool,
    ListSkillsTool,
    SearchSkillsTool,
    SkillManager,
)


def _run(async_fn):
    return asyncio.run(async_fn)


class TestCreateSkill:
    def test_create_and_persist(self, tmp_path):
        tool = CreateSkillTool()
        tool.manager.skills_dir = tmp_path

        result = _run(
            tool.execute(
                name="setup-flake8",
                description="Configure flake8 linter",
                content="pip install flake8\nflake8 .",
                tags=["python", "linting"],
            )
        )
        assert result.success is True
        assert "setup-flake8.md" in result.content

        # Verify file was written
        skill_file = tmp_path / "setup-flake8.md"
        assert skill_file.exists()
        text = skill_file.read_text()
        assert "flake8" in text
        assert "linting" in text


class TestListSkills:
    def test_empty_list(self, tmp_path):
        tool = ListSkillsTool()
        tool.manager.skills_dir = tmp_path
        result = _run(tool.execute())
        assert result.success is True
        assert "No skills" in result.content

    def test_lists_skills(self, tmp_path):
        # Pre-create a skill file
        (tmp_path / "setup-eslint.md").write_text(
            "---\nname: setup-eslint\ndescription: Configure ESLint\ntags: [js]\n---\n\nnpm install eslint\n"
        )
        tool = ListSkillsTool()
        tool.manager.skills_dir = tmp_path
        result = _run(tool.execute())
        assert result.success is True
        assert "setup-eslint" in result.content
        assert "Configure ESLint" in result.content


class TestSearchSkills:
    def test_search_by_tag(self, tmp_path):
        (tmp_path / "setup-pytest.md").write_text(
            "---\nname: setup-pytest\ndescription: Configure pytest\ntags: [python, testing]\n---\n\npip install pytest\n"
        )
        tool = SearchSkillsTool()
        tool.manager.skills_dir = tmp_path
        result = _run(tool.execute(query="testing"))
        assert result.success is True
        assert "setup-pytest" in result.content

    def test_search_no_match(self, tmp_path):
        tool = SearchSkillsTool()
        tool.manager.skills_dir = tmp_path
        result = _run(tool.execute(query="nonexistent"))
        assert result.success is True
        assert "No skills found" in result.content


class TestSkillManager:
    def test_load_skill(self, tmp_path):
        (tmp_path / "test-skill.md").write_text(
            "---\nname: test-skill\ndescription: A test\ntags: [a, b]\n---\n\nContent here.\n"
        )
        mgr = SkillManager(skills_dir=tmp_path)
        skill = mgr.load_skill("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test"
        assert skill.tags == ["a", "b"]
        assert "Content here" in skill.content

    def test_activate_skills(self, tmp_path):
        (tmp_path / "test.md").write_text(
            "---\nname: test-skill\ndescription: A test\ntags: [a]\n---\n\nDo this.\n"
        )
        mgr = SkillManager(skills_dir=tmp_path)
        prompt = mgr.activate_skills("test")
        assert "Skill: test-skill" in prompt
        assert "Do this" in prompt
