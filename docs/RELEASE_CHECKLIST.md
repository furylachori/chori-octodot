# Release Gate G2 Checklist

## Gate Objective
Gate G2 verifies the complete, offline core implementation of `chori-octodot` at the final release candidate revision before any live API interactions (G3–G6) are authorized.

All verification steps must run **offline** with **zero network access** and **zero secret credentials**.

---

## 1. Prerequisites and Environment Verification

- [ ] Working branch is verified (e.g. `impl/octodot-core`).
- [ ] Working tree is clean of uncommitted debug files, temporary artifacts, and databases.
- [ ] Python 3.10+ environment available.

Verify standard library compilation across all source files:
```bash
python3 -m compileall -q src
```

Verify CLI entrypoint availability and help text:
```bash
PYTHONPATH=src python3 -m octodot --help
```

---

## 2. Core Offline Test Suites

Execute each component test suite using the standard library `unittest` runner:

### A. Integration Suite (S14-T03)
Verifies end-to-end CLI read plans, `prepare --validate-only`, disabled template gating before credentials, fake-grant mutation pipelines (reply, create, approve) with $\le 1$ POST, crash/restart unknown reconciliation, and resumed wait workflows.
```bash
python3 -m unittest discover -s tests/integration -p "test_*.py" -v
```

### B. Compatibility and Shorthand Suite (S14-T02)
Verifies that shorthand commands compile to valid `jules-controller.plan.v1` read-only plans, shorthand mutations are rejected with `AUTH_DENIED`, and default registry blocks mutation dispatches without a verified grant.
```bash
python3 -m unittest discover -s tests/compat -p "test_*.py" -v
```

### C. Publication Safety Suite (S14-T04, S14-T05)
Verifies complete network socket and urllib isolation (monkeypatched to raise), zero credential access during read/prepare invocations, zero POSTs in read-only mode, and tracked files hygiene over `git ls-files`.
```bash
python3 -m unittest discover -s tests/publication_safety -p "test_*.py" -v
```

---

## 3. Python 3.10 Compatibility Matrix Check

Verify that all tests pass without using Python 3.11+ syntax or standard library features (no `tomllib`, `typing.Self`, `StrEnum`, `datetime.UTC`, or `ExceptionGroup`):
```bash
uv run --no-project --python 3.10 python -m unittest discover -s tests/integration -p "test_*.py" -v
uv run --no-project --python 3.10 python -m unittest discover -s tests/compat -p "test_*.py" -v
uv run --no-project --python 3.10 python -m unittest discover -s tests/publication_safety -p "test_*.py" -v
```

---

## 4. Aggregate Test Suite Verification (Guarded)

Execute the full repository test suite across all 15 implementation slices (S00–S14) with bounded test execution:
```bash
python3 -m unittest discover -s tests -v
```

Or under the bounded runner with 30s per-test SIGALRM:
```bash
python3 tests/integration/bounded_runner.py discover -s tests -p "test_*.py" -v
```

---

## 5. Tracked Files and Security Hygiene Check

Ensure no unredacted tokens, databases, machine paths, or license metadata are tracked:

1. **Verify no SQLite or state databases tracked**:
   ```bash
   git ls-files | grep -E '\.(sqlite|sqlite3|db|lock|log|jsonl)$' || echo "Clean"
   ```
2. **Verify no absolute developer machine paths tracked**:
   ```bash
   git grep -E '/(?:Users|home)/[a-zA-Z0-9_\-\.]+/' || echo "Clean"
   ```
3. **Verify no unredacted secret tokens tracked**:
   ```bash
   git grep -E '\b(gho_|ghp_|AIza|sk-)[A-Za-z0-9_\-]{20,}\b' || echo "Clean"
   ```
4. **Verify license absence**:
   Ensure `LICENSE`, `LICENSE.md`, `COPYING` do not exist, and no license fields appear in `pyproject.toml`.
   ```bash
   ls -la LICENSE* COPYING* 2>/dev/null || echo "No license file present (as required)"
   ```

---

## 6. Gate G2 Sign-Off Criteria

Gate G2 is marked **PASSED** when:
- [x] All offline unit, contract, and integration tests pass (100% pass rate).
- [x] Python 3.10 standard library compatibility verified.
- [x] Live mutations remain disabled by default (`DisabledGrantVerifier`).
- [x] Shorthand commands strictly enforce read-only execution.
- [x] Zero network calls occurred in the offline test suite.
- [x] Repository hygiene scan confirms zero database files, absolute paths, secrets, or license metadata.
