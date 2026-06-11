"""Tests for the CodmapGenerator (PR-05)."""

import time

import pytest

from index.codmap import (
    CodmapGenerator,
    FileEntry,
    extract_js_symbols,
    extract_python_symbols,
)

# ── FileEntry ──────────────────────────────────────────────────────


class TestFileEntry:
    def test_header_line_basic(self):
        e = FileEntry(path="src/x.py", line_count=120, mtime=time.time())
        line = e.header_line()
        assert "src/x.py" in line
        assert "120 lines" in line
        assert "ago" in line or "now" in line

    def test_age_seconds(self):
        e = FileEntry(path="x.py", line_count=10, mtime=time.time() - 30)
        assert "now" in e._age_str() or "ago" in e._age_str()

    def test_age_minutes(self):
        e = FileEntry(path="x.py", line_count=10, mtime=time.time() - 600)
        assert "10m" in e._age_str()

    def test_age_hours(self):
        e = FileEntry(path="x.py", line_count=10, mtime=time.time() - 7200)
        assert "2h" in e._age_str()

    def test_age_days(self):
        e = FileEntry(path="x.py", line_count=10, mtime=time.time() - 86400 * 3)
        assert "3d" in e._age_str()

    def test_age_months(self):
        e = FileEntry(path="x.py", line_count=10, mtime=time.time() - 86400 * 60)
        assert "2mo" in e._age_str()

    def test_render_includes_symbols(self):
        e = FileEntry(
            path="x.py",
            line_count=100,
            mtime=time.time(),
            symbols=["class Foo:", "def bar()", "baz = …"],
        )
        lines = e.render()
        assert lines[0].startswith("x.py")
        assert any("class Foo:" in l for l in lines)
        assert any("def bar()" in l for l in lines)

    def test_render_caps_symbols(self):
        e = FileEntry(
            path="x.py",
            line_count=10,
            mtime=time.time(),
            symbols=[f"def fn{i}()" for i in range(20)],
        )
        lines = e.render(max_symbols=3)
        symbol_lines = [l for l in lines if l.startswith("  ")]
        assert len(symbol_lines) == 3


# ── Python symbol extraction ──────────────────────────────────────


class TestPythonSymbolExtraction:
    def test_simple_function(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def hello():\n    pass\n")
        syms = extract_python_symbols(f)
        assert "def hello()" in syms

    def test_class_with_methods(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("class Auth:\n    def verify(self):\n        pass\n")
        syms = extract_python_symbols(f)
        assert "class Auth:" in syms

    def test_function_with_args(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def add(a, b, c=1):\n    return a + b + c\n")
        syms = extract_python_symbols(f)
        assert any("def add(" in s for s in syms)
        assert any("a" in s for s in syms if "def add" in s)

    def test_function_with_return_annotation(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def f(x: int) -> bool:\n    return True\n")
        syms = extract_python_symbols(f)
        sig = next(s for s in syms if "def f" in s)
        assert "->" in sig
        assert "bool" in sig

    def test_async_function(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("async def fetch():\n    pass\n")
        syms = extract_python_symbols(f)
        assert any("async def fetch" in s for s in syms)

    def test_top_level_variable(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("FOO = 42\n_bar = 'private'\n")
        syms = extract_python_symbols(f)
        # Public var should appear, private should not
        assert any("FOO" in s for s in syms)
        assert not any("_bar" in s for s in syms)

    def test_syntax_error_returns_empty(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def broken(:\n    pass\n")
        syms = extract_python_symbols(f)
        assert syms == []

    def test_missing_file(self, tmp_path):
        f = tmp_path / "missing.py"
        syms = extract_python_symbols(f)
        assert syms == []


# ── JS/TS symbol extraction ───────────────────────────────────────


class TestJsSymbolExtraction:
    def test_function(self, tmp_path):
        f = tmp_path / "x.js"
        f.write_text("function greet(name) { return name; }\n")
        syms = extract_js_symbols(f)
        assert any("function greet" in s for s in syms)

    def test_class(self, tmp_path):
        f = tmp_path / "x.js"
        f.write_text("class User {\n  constructor() {}\n}\n")
        syms = extract_js_symbols(f)
        assert any("class User" in s for s in syms)

    def test_exported_function(self, tmp_path):
        f = tmp_path / "x.ts"
        f.write_text("export function process() {}\n")
        syms = extract_js_symbols(f)
        assert any("function process" in s for s in syms)

    def test_empty_file(self, tmp_path):
        f = tmp_path / "x.js"
        f.write_text("")
        assert extract_js_symbols(f) == []


# ── CodmapGenerator ───────────────────────────────────────────────


class TestCodmapGenerator:
    @pytest.fixture
    def sample_workspace(self, tmp_path):
        """Create a small workspace with Python and JS files."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "def alpha():\n    pass\n\n" "def beta(x: int) -> bool:\n    return True\n"
        )
        (tmp_path / "src" / "b.py").write_text("class Foo:\n    def bar(self):\n        pass\n")
        (tmp_path / "src" / "app.js").write_text(
            "function hello() { return 1; }\n" "class App {}\n"
        )
        # File in skip dir — should be ignored
        (tmp_path / "src" / "__pycache__").mkdir()
        (tmp_path / "src" / "__pycache__" / "x.py").write_text("# skip me")
        # Non-code file
        (tmp_path / "README.md").write_text("# readme")
        return tmp_path

    def test_empty_workspace_returns_empty(self, tmp_path):
        gen = CodmapGenerator(workspace=tmp_path)
        assert gen.generate() == ""

    def test_generates_for_python_files(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        assert "src/a.py" in text
        assert "src/b.py" in text

    def test_includes_symbols(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        assert "alpha" in text  # function name from a.py
        assert "Foo" in text  # class name from b.py

    def test_skips_pycache(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        assert "__pycache__" not in text

    def test_skips_non_code_files(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        assert "README.md" not in text

    def test_includes_js_files(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        assert "src/app.js" in text

    def test_sorted_by_mtime_descending(self, sample_workspace):
        # Modify a.py to make it most recent (writing changes mtime even if
        # content is the same; some filesystems have 1s mtime granularity).
        a_path = sample_workspace / "src" / "a.py"
        a_path.write_text(a_path.read_text() + "\n# touched\n")
        gen = CodmapGenerator(workspace=sample_workspace)
        text = gen.generate()
        a_idx = text.find("src/a.py")
        b_idx = text.find("src/b.py")
        # a.py was just modified, should appear before b.py
        assert 0 <= a_idx < b_idx

    def test_respects_max_files(self, sample_workspace):
        # Add 10 more files
        for i in range(10):
            (sample_workspace / "src" / f"f{i}.py").write_text(f"x = {i}\n")
        gen = CodmapGenerator(workspace=sample_workspace, max_files=3)
        text = gen.generate()
        # Count of "lines" in headers — should be ≤ 3
        header_count = sum(1 for line in text.split("\n") if "lines" in line)
        assert header_count <= 3

    def test_respects_max_total_kb(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace, max_total_kb=1)
        text = gen.generate()
        assert len(text.encode("utf-8")) <= 1024 * 2  # within budget + first file

    def test_cache_hits_skip_reparse(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        gen.generate()
        cache_size_after_first = gen.cache_size()
        # Second generate with no file changes → cache should not grow
        gen.generate()
        assert gen.cache_size() == cache_size_after_first

    def test_cache_invalidates_on_mtime_change(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        gen.generate()
        first_text = gen.generate()
        # Modify a file (changes mtime)
        time.sleep(0.01)
        (sample_workspace / "src" / "a.py").write_text("# updated\ndef new_fn():\n    pass\n")
        second_text = gen.generate()
        # The new function should appear, the old content may not
        assert "new_fn" in second_text
        # File entries should be re-extracted
        assert first_text != second_text or "new_fn" in second_text

    def test_clear_cache(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace)
        gen.generate()
        assert gen.cache_size() > 0
        gen.clear_cache()
        assert gen.cache_size() == 0

    def test_handles_empty_workspace_with_no_code_files(self, tmp_path):
        (tmp_path / "data.json").write_text("{}")
        gen = CodmapGenerator(workspace=tmp_path)
        assert gen.generate() == ""

    def test_works_with_max_symbols_per_file(self, sample_workspace):
        gen = CodmapGenerator(workspace=sample_workspace, max_symbols_per_file=1)
        text = gen.generate()
        # Per file, at most 1 indented symbol line. Files are separated by
        # header lines (no blank lines), so we group: a header line + 0-1
        # following indented lines form one block.
        lines = text.split("\n")
        # Each file's block: header, then up to N indented lines
        # Walk: a header line begins a block; following indented lines belong
        # to it until the next non-indented line.
        blocks = []
        i = 0
        while i < len(lines):
            if lines[i] and not lines[i].startswith(" "):
                # new header
                block = [lines[i]]
                j = i + 1
                while j < len(lines) and lines[j].startswith("  "):
                    block.append(lines[j])
                    j += 1
                blocks.append(block)
                i = j
            else:
                i += 1
        for block in blocks:
            indented = [l for l in block if l.startswith("  ")]
            assert len(indented) <= 1
