"""Pytest configuration for all tests."""

import os
import sys

# Enable testing mode to relax workspace path restrictions
os.environ["CODING_AGENT_TESTING"] = "1"