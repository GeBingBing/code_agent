"""Tests for code indexer semantic features: references, call graph, related symbols."""

from pathlib import Path

from index.code_indexer import CodeIndexer

SAMPLE_PY = """
class AuthManager:
    def __init__(self):
        self.token = None

    def login(self, user):
        self.token = self._generate_token(user)
        return self.token

    def _generate_token(self, user):
        return f"token_for_{user}"

    def logout(self):
        self.token = None

def get_auth_manager():
    return AuthManager()

def main():
    auth = get_auth_manager()
    auth.login("alice")
    auth.logout()
"""

SAMPLE_B = """
from auth import AuthManager

def verify_user():
    auth = AuthManager()
    return auth.login("bob")
"""


class TestReferences:
    def test_find_references_same_file(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        refs = idx.find_references("login")
        # login is called in main() and verify_user (from sample_b not indexed yet)
        paths = [r["path"] for r in refs]
        assert any("auth.py" in p for p in paths)

    def test_find_references_cross_file(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        (tmp_path / "verify.py").write_text(SAMPLE_B)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        refs = idx.find_references("login")
        paths = [r["path"] for r in refs]
        assert any("auth.py" in p for p in paths)
        assert any("verify.py" in p for p in paths)

    def test_find_references_excludes_definition(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        refs = idx.find_references("AuthManager")
        # Should not include the class definition line
        for r in refs:
            assert "class AuthManager" not in r["context"]


class TestCallGraph:
    def test_build_call_graph(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        graph = idx.build_call_graph()
        # Methods are stored with class prefix
        assert "AuthManager.login" in graph
        # login calls _generate_token
        called_names = [c["name"] for c in graph["AuthManager.login"]]
        assert "AuthManager._generate_token" in called_names

    def test_main_calls_login(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        graph = idx.build_call_graph()
        assert "main" in graph
        called_names = [c["name"] for c in graph["main"]]
        assert "get_auth_manager" in called_names
        assert "AuthManager.login" in called_names or "AuthManager.logout" in called_names


class TestRelatedSymbols:
    def test_same_class_methods(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        related = idx.get_related_symbols("AuthManager.login")
        same_class = [r for r in related if r["relation"] == "same_class"]
        names = [r["name"] for r in same_class]
        assert "AuthManager.logout" in names

    def test_called_by(self, tmp_path: Path):
        (tmp_path / "auth.py").write_text(SAMPLE_PY)
        idx = CodeIndexer(str(tmp_path))
        idx.index_project(patterns=["*.py"])

        related = idx.get_related_symbols("AuthManager._generate_token")
        callers = [r for r in related if r["relation"] == "called_by"]
        names = [r["name"] for r in callers]
        assert "AuthManager.login" in names
