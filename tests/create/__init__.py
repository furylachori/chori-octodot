"""Tasks create test suite package initialization.

Bootstraps sys.path to include <repo>/src.
"""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import types
if "octodot.cli" not in sys.modules:
    try:
        import octodot.cli
    except (ImportError, ModuleNotFoundError):
        _cli = types.ModuleType("octodot.cli")
        _cli.main = lambda *args, **kwargs: 0
        sys.modules["octodot.cli"] = _cli

if "octodot.runner" not in sys.modules:
    try:
        import octodot.runner
    except (ImportError, ModuleNotFoundError):
        _runner = types.ModuleType("octodot.runner")
        _runner.ActionRunner = None
        _runner.run_plan = None
        sys.modules["octodot.runner"] = _runner
