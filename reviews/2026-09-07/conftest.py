"""Reuse fabricated fixtures for the independent audit, outside the product test suite."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
