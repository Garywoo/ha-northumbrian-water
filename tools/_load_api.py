"""Load the api module without importing the Home Assistant package around it.

``custom_components/northumbrian_water/__init__.py`` imports Home Assistant, so
a plain ``import northumbrian_water.api`` would need HA installed. The API client
itself has no such dependency, so load it straight from its file instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "northumbrian_water"
    / "api.py"
)


def load_api() -> ModuleType:
    """Import and return the api module."""
    spec = importlib.util.spec_from_file_location("nwl_api", API_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {API_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwl_api"] = module
    spec.loader.exec_module(module)
    return module
