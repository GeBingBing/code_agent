"""Tests for persistent code indexer."""

import json
import tempfile
from pathlib import Path

import pytest

from index.code_indexer import CodeIndexer


class TestPersistentIndex:
    """Test SQLite-backed persistent code index."""

    def test_index_save_and_load(self, tmp_path):
        """Index should persist to disk and reload on restart."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "test.py").write_text("def foo(): pass\nclass Bar: pass")

        # First: index
        indexer = CodeIndexer(str(src))
        indexer.index_project()

        # Save to disk
        db_path = tmp_path / "index.json"
        indexer.save(str(db_path))

        # Load from disk
        indexer2 = CodeIndexer(str(src))
        indexer2.load(str(db_path))

        # Should have same file count
        assert len(indexer2.files) == len(indexer.files)

    def test_save_load_preserves_symbols(self, tmp_path):
        """Reloaded index should preserve symbol information."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "test.py").write_text("def my_func(): pass\nclass MyClass: pass")

        indexer = CodeIndexer(str(src))
        indexer.index_project()

        db_path = tmp_path / "index.json"
        indexer.save(str(db_path))

        indexer2 = CodeIndexer(str(src))
        indexer2.load(str(db_path))

        # Should have symbols
        file_key = list(indexer2.files.keys())[0]
        symbols = indexer2.files[file_key].symbols
        names = [s.name for s in symbols]
        assert "my_func" in names or "MyClass" in names

    def test_incremental_update(self, tmp_path):
        """Adding new files should update index without full re-index."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("def a(): pass")

        indexer = CodeIndexer(str(src))
        indexer.index_project()
        db_path = tmp_path / "index.json"
        indexer.save(str(db_path))

        # Add new file
        (src / "b.py").write_text("def b(): pass")

        # Load and update
        indexer2 = CodeIndexer(str(src))
        indexer2.load(str(db_path))
        indexer2.index_project(patterns=["*.py"])  # incremental

        # Should have 2 files
        assert len(indexer2.files) == 2