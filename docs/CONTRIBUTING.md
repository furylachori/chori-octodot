# Contributing to chori-octodot

Thank you for your interest in contributing to `chori-octodot`. This document provides an overview of the project layout, key modules, and how to run the test suite.

## Project Layout

- `src/octodot/` - The core implementation of the portable Jules controller.
- `tests/` - Comprehensive test suite containing unit, integration, and offline verification tests.
- `docs/` - Project documentation, including operation guides, architecture, and contracts.
- `schemas/` - JSON schemas for validation.
- `plan/` - Implementation plan artifacts and tracking.
- `examples/` - Example configurations or usage samples.

## Key Modules in `src/octodot`

- `contracts.py` - Core typed interfaces (`typing.Protocol`), fixed operation inventory, error codes, and shared data structures.
- `store.py` - Durable SQLite storage layer, managing short transactions, owner locking, operations, events, and receipts.
- `journal.py` - Single-attempt mutation journal, providing opaque dispatch tickets and outcome recording.
- `authorization.py` - Enforces the security boundary, grant verification, and the fail-closed default posture (`DisabledGrantVerifier`).
- `runner.py` - Ordered JSON runner that executes structured plans (`jules-controller.plan.v1`).
- `api.py` / `reads.py` - Interactions with the Jules API for collecting and inspecting resources.
- `cli.py` - The main command-line interface entry point.
- `compat.py` - Implementation of read-only compatibility shorthands.

## Running Tests (Python 3.10+)

The test suite runs entirely offline using the standard library `unittest` module. No network access or live credentials are required (or permitted).

### Standard Execution

To run all tests:
```bash
python3 -m unittest discover -s tests -v
```

### Specific Test Suites

You can run individual suites, such as the integration suite:
```bash
python3 -m unittest discover -s tests/integration -p "test_*.py" -v
```

### Bounded Test Runner

To ensure tests do not hang indefinitely, a bounded runner enforcing a 30-second POSIX `SIGALRM` per-test deadline is available:
```bash
python3 tests/integration/bounded_runner.py discover -s tests -p "test_*.py" -v
```

All contributions must pass the test suite under Python 3.10+ using standard library syntax.
