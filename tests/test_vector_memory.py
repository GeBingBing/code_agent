"""Tests for VectorMemory."""

import numpy as np
import pytest
from pathlib import Path
import tempfile

from agent.core.vector_memory import (
    VectorMemory, simple_text_hash, cosine_similarity,
    get_vector_memory, reset_vector_memory
)


class TestSimpleTextHash:
    """Test the simple text hash function."""

    def test_same_text_same_hash(self):
        h1 = simple_text_hash("hello world")
        h2 = simple_text_hash("hello world")
        assert np.allclose(h1, h2)

    def test_different_text_different_hash(self):
        h1 = simple_text_hash("hello")
        h2 = simple_text_hash("world")
        # Not guaranteed to be different, but very likely
        assert not np.allclose(h1, h2)

    def test_normalized_vector(self):
        h = simple_text_hash("test")
        norm = np.linalg.norm(h)
        assert abs(norm - 1.0) < 0.001

    def test_empty_text(self):
        h = simple_text_hash("")
        assert np.allclose(h, np.zeros(128))

    def test_chinese_text(self):
        h = simple_text_hash("你好世界")
        assert np.allclose(h, simple_text_hash("你好世界"))
        assert np.linalg.norm(h) > 0


class TestCosineSimilarity:
    """Test cosine similarity function."""

    def test_identical_vectors(self):
        v = np.array([1.0, 0.0])
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        v1 = np.array([1.0, 0.0])
        v2 = np.array([0.0, 1.0])
        assert cosine_similarity(v1, v2) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        v1 = np.array([1.0, 0.0])
        v2 = np.array([-1.0, 0.0])
        assert cosine_similarity(v1, v2) == pytest.approx(-1.0)


class TestVectorMemory:
    """Test the VectorMemory class."""

    @pytest.fixture
    def mem(self, tmp_path):
        """Create a temporary vector memory."""
        reset_vector_memory()
        vm = VectorMemory(db_path=tmp_path / "test.db")
        yield vm
        vm.clear()

    def test_add_and_get(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        vm.add("key1", "value1")

        result = vm.get("key1")
        assert result == "value1"

    def test_search(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        vm.add("python_file", "def hello(): print('hello')")
        vm.add("java_file", "public class Main {}")
        vm.add("readme", "This is a readme file")

        results = vm.search("python function", top_k=2)
        assert len(results) <= 2
        # Python file should rank higher for "python function" query
        keys = [r[0] for r in results]
        assert "python_file" in keys

    def test_get_nonexistent(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        result = vm.get("nonexistent")
        assert result is None

    def test_get_all_keys(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        vm.add("key1", "value1")
        vm.add("key2", "value2")

        keys = vm.get_all_keys()
        assert set(keys) == {"key1", "key2"}

    def test_count(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        assert vm.count() == 0
        vm.add("key1", "value1")
        assert vm.count() == 1
        vm.add("key2", "value2")
        assert vm.count() == 2

    def test_clear(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        vm.add("key1", "value1")
        vm.add("key2", "value2")
        assert vm.count() == 2

        vm.clear()
        assert vm.count() == 0

    def test_search_returns_key_value_similarity(self, tmp_path):
        vm = VectorMemory(db_path=tmp_path / "test.db")
        vm.add("test_key", "test value")

        results = vm.search("test query")
        assert len(results) > 0
        key, value, sim = results[0]
        assert isinstance(key, str)
        assert isinstance(value, str)
        assert isinstance(sim, float)
        assert 0 <= sim <= 1


class TestVectorMemorySingleton:
    """Test the global singleton functions."""

    def setup_method(self):
        reset_vector_memory()

    def test_get_vector_memory(self):
        vm = get_vector_memory()
        assert vm is not None
        assert isinstance(vm, VectorMemory)

    def test_same_instance(self):
        vm1 = get_vector_memory()
        vm2 = get_vector_memory()
        assert vm1 is vm2

    def test_reset(self):
        vm1 = get_vector_memory()
        vm1.add("test", "value")
        reset_vector_memory()
        vm2 = get_vector_memory()
        assert vm2.count() == 0