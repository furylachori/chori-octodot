# Operations and Deployment Guide

## Overview

`chori-octodot` (`jules-controller`) is a portable controller for the Google Jules API. It executes structured plans (`jules-controller.plan.v1`), provides durable SQLite-backed event sourcing and reconciliation, and enforces strict authorization and single-attempt mutation guarantees.

The runtime requires **Python 3.10+ standard library only**. There are zero third-party runtime dependencies.

---

## 1. Installation and Execution

### Requirements
- Python 3.10, 3.11, 3.12, or 3.13.
- Standard POSIX operating system (Linux, macOS). Windows is unsupported.

### Installation
From the repository root:
```bash
# Editable install
pip install -e .

# Or standard install
pip install .

# Development dependencies (jsonschema for strict vector tests)
pip install -r requirements-dev.txt
```

### CLI Invocations
The console script `jules-controller` is registered via `pyproject.toml`:
```bash
# Via console script
jules-controller --help

# Or via Python module directly
python3 -m octodot --help
```

### Basic Commands
1. **Run a plan**:
   ```bash
   jules-controller run --plan plan.json --result result.json --state-dir ~/.octodot
   ```
2. **Validate plan offline (prepare --validate-only)**:
   ```bash
   jules-controller prepare --validate-only --plan plan.json --result report.json
   ```
3. **Execute shorthand command**:
   ```python
   from octodot.compat import run_shorthand
   res = run_shorthand("status")
   ```

---

## 2. State Directory and Recovery Fence Placement

### State Directory (`--state-dir`)
- Defaults to `~/.octodot` if omitted.
- Contains the private durable SQLite database (`state.db`) and lock file (`state.db.lock`).
- File permissions are initialized with `0700` (`rwx------`).
- Runtime state, credentials, private conversation tokens, and unredacted customer data never belong in git.

### Recovery Fence Placement
- The **Recovery Fence** tracks the host-controlled configuration epoch (`profile_epoch`) and checkpoint sequencing.
- **CRITICAL**: The recovery fence file MUST be placed **OUTSIDE** the `--state-dir` (e.g. `/etc/octodot/fence.json` or managed by an external host authority).
- If the state directory is backed up and later restored, placing the fence outside the state directory ensures that the host fence epoch remains ahead of the restored database.
- Attempting to dispatch mutations from a restored database with a stale epoch triggers `ErrorCode.RECOVERY_FENCE_STALE` (exit code 4), preventing double-dispatch of mutations across rollbacks or VM restores.

---

## 3. Exit Codes and Deterministic Precedence

The controller uses deterministic integer exit codes:

| Exit Code | Constant | Meaning |
|---|---|---|
| **0** | `EXIT_OK` | Complete / success. All actions succeeded. |
| **2** | `EXIT_WAITING` | Yielded / waiting. Polling or async predicate active. Resume token generated. |
| **3** | `EXIT_FATAL_READ_OR_LOCAL` | Invalid input, schema violation, or fatal local/read error. |
| **4** | `EXIT_MUTATION_BLOCKED` | Mutation blocked before dispatch, rejected, or uncertain effect. |
| **5** | `EXIT_PARTIAL_OR_UNSUPPORTED` | Partial coverage, missing pagination, or explicit unsupported op. |
| **130** | `EXIT_INTERRUPTED` | Interrupted by signal (`SIGINT`, `KeyboardInterrupt`). |

### Multi-Action Precedence Rule
When an invocation contains multiple actions with different outcomes, the process exit code follows strict precedence:
$$\mathbf{130} > \mathbf{4} > \mathbf{3} > \mathbf{5} > \mathbf{2} > \mathbf{0}$$

Every individual action result and error detail is preserved in the output result document (`jules-controller.result.v1`).

---

## 4. Invocation Caps and Single-Attempt Writes

Every invocation operates within strictly bounded limits:
- **Max HTTP requests**: 120 per read invocation; exactly 1 POST per mutation action.
- **Request timeout**: 20 seconds.
- **Overall invocation deadline**: 180 seconds.
- **Max pagination pages**: 100 pages.
- **Max session records**: 200 sessions.
- **Max response payload**: 8 MiB per HTTP response.
- **Max total bytes**: 32 MiB total across all HTTP requests in an invocation.
- **Max output result bytes**: 64 KiB.

### Single-Attempt Mutation Rule
- At most **one** HTTP POST is attempted for any mutation (`chats.reply`, `tasks.create`, `plans.approve`).
- **Zero blind retries**: If a POST request experiences network disconnection, timeout, or server error, the operation state transitions to `UNKNOWN`. It is **never** resent automatically.
- Read-only reconciliation (`operations.reconcile`) can scan for observed effects without issuing writes.

---

## 5. Trusted Authorization and Disabled Live Writes

### Default Security Posture
- In the offline core release, live API mutations are **disabled by default**.
- The default registry constructor uses `DisabledGrantVerifier`, which fails closed and returns `ErrorCode.VERIFIER_UNAVAILABLE` (exit code 4) on any mutation dispatch attempt.
- `FakeGrantVerifier` is strictly marked `FIXTURE_ONLY` and cannot be paired with live network transports.
- Shorthand commands (`inventory`, `inspect`, `chats`, `events`, `ack`, `wait`, `reconcile`, `status`) strictly compile with `execution.mode = "read_only"`. Any mutation shorthand (`send`, `reply`, `create`, `approve`) is rejected immediately with `ErrorCode.AUTH_DENIED`.
- Live writes require an external, explicitly integrated host `GrantVerifier` adapter, an explicit `jules-controller.plan.v1` plan, and a verified cryptographically bound grant.

---

## 6. Finite Test Execution and Bounded Runner

- All test cases run offline using synthetic fixtures, deterministic clocks (`FakeClock`), and in-memory or temporary SQLite databases.
- The test suite includes `tests/integration/bounded_runner.py`:
  - Enforces a 30-second POSIX `SIGALRM` per-test deadline.
  - Prevents tests from hanging indefinitely on locks, deadlocks, or infinite loops.
- In CI (`.github/workflows/offline.yml`), test suites are executed with individual 600-second timeouts, and the aggregate suite runs under `bounded_runner.py` within a 20-minute overall job timeout.

---

## 7. Known Limitations

1. **Cooperative DNS and Blocked I/O Deadlines**: Python standard library socket timeouts cannot interrupt long-running native `getaddrinfo` DNS lookups. In severely degraded network environments, connection timeouts may exceed deadlines cooperatively.
2. **Template Placeholder Scanner False Positives**: Strings containing literal `<...>` patterns or tokens like `TODO`, `CHANGEME`, or `REPLACE_ME` are rejected by the safety validator to prevent accidental execution of unedited templates.
3. **Secret and Consequential Directive Scanners**: Chat text is conservatively scanned for credential patterns (`gho_`, `ghp_`, `AIza`, `sk-`, `BEGIN PRIVATE KEY`) and dangerous phrases (`deploy to production`, `destroy infrastructure`). Code samples in chat containing these patterns will be blocked.
4. **Session-Level Non-Atomic Plan Approval**: Approving a plan via Jules API applies to the active plan in the session. Concurrent updates to the session between inspection and approval can cause plan hash mismatches, requiring a fresh inspection pass.
5. **No Exact-Commit Pinning**: Task creation (`tasks.create`) specifies a target repository and starting branch. Jules API checks out the tip of the starting branch at session initialization; exact commit SHA pinning is not exposed by the upstream public API.
6. **Suggested Tasks API Unsupported**: Google Jules Suggested Tasks endpoints are not part of the public supported contract in this release.
7. **Windows Unsupported**: The controller relies on POSIX signal handling (`SIGALRM`), octal directory permissions (`0700`), and Unix file locking semantics (`fcntl.flock`). Windows is unsupported.

---

## 8. Greenfield Implementation Note

This repository is a clean-room, greenfield implementation of the Jules controller core. Legacy experimental facades and undocumented relay commands are replaced by:
- Explicit typed contracts (`src/octodot/contracts.py`).
- JSON Schema verified execution plans (`jules-controller.plan.v1`).
- Static handler registry (`src/octodot/registry.py`).
- Read-only compatibility shorthands (`src/octodot/compat.py`).
- Durable crash-recovery SQLite storage with recovery fencing.
