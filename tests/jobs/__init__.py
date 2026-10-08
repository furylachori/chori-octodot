"""Jobs test package initialization.

Bootstraps sys.path to include <repo>/src (computed from __file__).
Idempotent, no other side effects.
"""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
