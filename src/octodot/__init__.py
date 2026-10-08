"""octodot package initialization.

Exports runner and CLI entry points. Standard library only.
"""

from __future__ import annotations

from octodot.runner import ActionRunner, run_plan
from octodot.cli import main

__all__ = [
    "ActionRunner",
    "run_plan",
    "main",
]
