"""Bounded unittest runner enforcing a per-test 30-second cap on POSIX.

Standard library only. Compatible with Python 3.10+.
Installs SIGALRM before each test case starts and clears it upon completion.
Used by offline CI and local test harnesses to guarantee termination.
"""

from __future__ import annotations

import os
import signal
import sys
import unittest
from typing import Any

# Bootstrap repo root and src/ into sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PER_TEST_TIMEOUT_SECONDS = 30


class TestTimeoutError(TimeoutError):
    """Raised when an individual test exceeds the per-test timeout cap."""


def _timeout_handler(signum: int, frame: Any) -> None:
    raise TestTimeoutError(
        f"Test exceeded the mandatory {PER_TEST_TIMEOUT_SECONDS}-second execution cap"
    )


class BoundedTextTestResult(unittest.TextTestResult):
    """Test result implementing per-test alarm scheduling."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prev_handler: Any = None
        self._has_alarm: bool = hasattr(signal, "SIGALRM") and hasattr(signal, "alarm")

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        if self._has_alarm:
            self._prev_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PER_TEST_TIMEOUT_SECONDS)

    def stopTest(self, test: unittest.TestCase) -> None:
        if self._has_alarm:
            signal.alarm(0)
            if self._prev_handler is not None:
                signal.signal(signal.SIGALRM, self._prev_handler)
                self._prev_handler = None
        super().stopTest(test)


class BoundedTextTestRunner(unittest.TextTestRunner):
    """Text test runner that enforces the per-test 30-second execution cap."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("resultclass", BoundedTextTestResult)
        super().__init__(*args, **kwargs)


def main() -> int:
    """Discover and run all tests under the bounded test runner."""
    start_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_REPO_ROOT, "tests")
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir)
    runner = BoundedTextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
