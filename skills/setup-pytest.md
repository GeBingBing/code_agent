---
name: setup-pytest
description: Configure pytest for a Python project with coverage
tags:
  - python
  - testing
  - pytest
---

# Setup Pytest

1. Install pytest and pytest-cov:
   ```bash
   pip install pytest pytest-cov
   ```

2. Create `pytest.ini`:
   ```ini
   [pytest]
   testpaths = tests
   python_files = test_*.py
   python_classes = Test*
   python_functions = test_*
   addopts = --cov=src --cov-report=term-missing
   ```

3. Create `tests/` directory and add `__init__.py`.

4. Run tests:
   ```bash
   pytest
   ```
