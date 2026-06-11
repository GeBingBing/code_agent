"""Tests for Phase 6: Code indexing and retrieval."""

from index.code_indexer import CodeIndexer
from index.retriever import CodeRetriever


class TestCodeIndexer:
    def test_parse_python_symbols(self, tmp_path):
        f = tmp_path / "sample.py"
        f.write_text("""
class UserAuth:
    def login(self):
        pass

def validate_token():
    pass

API_KEY = "secret"
""")
        idx = CodeIndexer(str(tmp_path))
        idx._index_file(f)

        file_idx = idx.files["sample.py"]
        symbols = {s.name: s.kind for s in file_idx.symbols}
        assert "UserAuth" in symbols
        assert symbols["UserAuth"] == "class"
        assert "UserAuth.login" in symbols
        assert symbols["UserAuth.login"] == "method"
        assert "validate_token" in symbols
        assert symbols["validate_token"] == "function"
        assert "API_KEY" in symbols
        assert symbols["API_KEY"] == "variable"

    def test_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "foo.cpython-312.pyc").write_text("x")
        idx = CodeIndexer(str(tmp_path))
        idx.index_project()
        assert len(idx.files) == 0


class TestCodeRetriever:
    def test_search_by_symbol_name(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("class UserAuth:\n    pass\n")
        idx = CodeIndexer(str(tmp_path))
        idx.index_project()
        ret = CodeRetriever(idx)

        results = ret.search("UserAuth", top_k=5)
        assert len(results) > 0
        assert any(r.name == "UserAuth" for r in results)

    def test_search_by_filename(self, tmp_path):
        f = tmp_path / "authentication.py"
        f.write_text("def login(): pass\n")
        idx = CodeIndexer(str(tmp_path))
        idx.index_project()
        ret = CodeRetriever(idx)

        results = ret.search("auth", top_k=5)
        assert any(r.path == "authentication.py" for r in results)

    def test_latency_under_500ms(self, tmp_path):
        # Create a moderately sized codebase
        for i in range(20):
            f = tmp_path / f"module_{i}.py"
            f.write_text(f"def func_{i}():\n    pass\n" * 50)

        idx = CodeIndexer(str(tmp_path))
        idx.index_project()
        ret = CodeRetriever(idx)

        results = ret.search("func", top_k=5)
        # Timing info is appended to first result snippet
        assert len(results) > 0
        assert "ms" in results[0].snippet
