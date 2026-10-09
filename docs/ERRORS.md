# Octodot Error Taxonomy and Exit Code Specification

This document details the closed error code taxonomy, exception hierarchy, exit codes, precedence rules, and error sanitization guarantees for `chori-octodot` (`jules-controller`).

---

## 1. Error Handling Architecture & Sanitization Guarantees

### 1.1 Closed Taxonomy
`chori-octodot` defines a closed set of string error codes in `octodot.errors.ErrorCode`. Any error produced by the controller core maps to an explicit `ErrorCode` member. Uncategorized or raw python exceptions are caught at boundary layers and converted to `ErrorCode.INTERNAL_ERROR` or specific domain exceptions.

### 1.2 Sanitization Policy
To prevent credential leaks, private token exposure, or data contamination in logs and execution results:
- **Zero Raw Payload Leakage**: Raw HTTP response bodies, raw headers, bearer tokens, API keys (`JULES_API_KEY`), and authorization grants are **never** included in exception messages, logs, or result documents (`jules-controller.result.v1`).
- **Structured Exception Formatting**: Exceptions inherit from `OctodotError` and format as `[{code}] {message}`.
- **Sanitized Transport Outcomes**: Transport errors sanitize upstream status codes and body fragments into safe human-readable summaries before raising or recording results.

---

## 2. Closed ErrorCode Taxonomy

The 40 `ErrorCode` values are organized into 7 functional categories:

### 2.1 Validation & Syntax

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `invalid_input` | `INVALID_INPUT` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Input parameters, CLI flags, schema fields, or arguments fail format, type, or constraint validation checks. |
| `duplicate_key` | `DUPLICATE_KEY` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Structured JSON input contains duplicate keys in a dictionary where unique keys are required. |
| `nonfinite_number` | `NONFINITE_NUMBER` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Input or payload contains nonfinite float values (`NaN`, `Infinity`, or `-Infinity`). |
| `unknown_field` | `UNKNOWN_FIELD` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Unknown or unexpected field encountered in a closed schema structure (`additionalProperties: false`). |
| `oversized_input` | `OVERSIZED_INPUT` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Input plan or serialized document exceeds maximum allowed byte size. |
| `invalid_reference` | `INVALID_REFERENCE` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Action selection reference target is missing, self-referential, forward-referencing, or references a non-read action. |
| `dynamic_mutation_target` | `DYNAMIC_MUTATION_TARGET` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Mutation action target or payload contains variable references or expressions instead of literal frozen values. |
| `placeholder_present` | `PLACEHOLDER_PRESENT` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Plan template contains unedited placeholder tokens (e.g. `<...>`, `TODO`, `CHANGEME`, `REPLACE_ME`). |
| `template_disabled` | `TEMPLATE_DISABLED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Action in plan template has `enabled: false`. |
| `schema_too_new` | `SCHEMA_TOO_NEW` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Plan or result schema version is newer than supported by this controller release. |

---

### 2.2 Unsupported Features & Limitations

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `unsupported_public_api` | `UNSUPPORTED_PUBLIC_API` | `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`) | Requested operation or resource (e.g., `suggestions.collect`) is not supported by the public Jules API. |
| `unsupported_exact_commit` | `UNSUPPORTED_EXACT_COMMIT` | `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`) | Exact commit SHA pinning requested, but upstream Jules API only supports branch-level binding. |
| `unsupported_atomic_plan_approval` | `UNSUPPORTED_ATOMIC_PLAN_APPROVAL` | `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`) | Atomic exact plan approval requested, but upstream API lacks a plan-version precondition on approval endpoints. |

---

### 2.3 Authorization & Credentials

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `auth_denied` | `AUTH_DENIED` | `4` (`EXIT_MUTATION_BLOCKED`) | Action or command rejected due to execution mode restrictions (e.g. attempting mutation in `read_only` mode). |
| `grant_missing` | `GRANT_MISSING` | `4` (`EXIT_MUTATION_BLOCKED`) | No execution grant provided for an authorized mutation operation. |
| `grant_invalid` | `GRANT_INVALID` | `4` (`EXIT_MUTATION_BLOCKED`) | Grant token or binding failed signature, hash, or scope verification checks. |
| `grant_expired` | `GRANT_EXPIRED` | `4` (`EXIT_MUTATION_BLOCKED`) | Grant token TTL has expired. |
| `grant_revoked` | `GRANT_REVOKED` | `4` (`EXIT_MUTATION_BLOCKED`) | Grant token was explicitly revoked or invalidated by authority. |
| `verifier_unavailable` | `VERIFIER_UNAVAILABLE` | `4` (`EXIT_MUTATION_BLOCKED`) | Grant verifier is disabled or unconfigured (e.g., default live CLI posture with `DisabledGrantVerifier`). |
| `recovery_fence_stale` | `RECOVERY_FENCE_STALE` | `4` (`EXIT_MUTATION_BLOCKED`) | Host recovery fence epoch does not match stored state, blocking mutation dispatch to prevent double-writes across VM/database restores. |

---

### 2.4 Transport & Network

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `rate_limited` | `RATE_LIMITED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Upstream Jules API returned HTTP 429 Rate Limit error. |
| `transport_error` | `TRANSPORT_ERROR` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Network connection failed, DNS resolution failed, or remote server returned HTTP 5xx error. |
| `timeout` | `TIMEOUT` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Request or invocation exceeded allowed timeout deadline (e.g. 20s request / 180s total). |
| `uncertain_effect` | `UNCERTAIN_EFFECT` | `4` (`EXIT_MUTATION_BLOCKED`) | Mutation POST request timed out or disconnected before receiving a verified response. Status on remote server is unknown; operation marked `UNKNOWN` with zero blind retries. |
| `malformed_response` | `MALFORMED_RESPONSE` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Upstream HTTP response body is invalid JSON or violates expected protocol schema. |
| `oversized_response` | `OVERSIZED_RESPONSE` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Upstream HTTP response exceeds byte limits (8 MiB per response / 32 MiB total / 64 KiB output result). |
| `budget_exhausted` | `BUDGET_EXHAUSTED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`) | Invocation exceeded max request count (120 requests) or byte transfer budgets. |

---

### 2.5 Identity, Scope & Coverage

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `partial_coverage` | `PARTIAL_COVERAGE` | `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`) | Read query returned incomplete data or reached pagination limits without completing coverage. |
| `identity_ambiguous` | `IDENTITY_AMBIGUOUS` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Target session or source identifier resolved to multiple ambiguous candidates. |
| `binding_mismatch` | `BINDING_MISMATCH` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Scope binding mismatch between plan, store, repository, branch, or session. |
| `branch_unverified` | `BRANCH_UNVERIFIED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `4` (`EXIT_MUTATION_BLOCKED`) | Starting branch or target repository branch absent or unverified against remote sources. |
| `unknown_state` | `UNKNOWN_STATE` | `4` (`EXIT_MUTATION_BLOCKED`) | Session or operation state is unrecognized or cannot be reconciled safely. |
| `accepted_identity_unverified` | `ACCEPTED_IDENTITY_UNVERIFIED` | `4` (`EXIT_MUTATION_BLOCKED`) | Task creation accepted upstream, but subsequent session identity verification failed; operation marked `UNKNOWN` with flag set (must never recreate automatically). |

---

### 2.6 State & Journal

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `operation_conflict` | `OPERATION_CONFLICT` | `4` (`EXIT_MUTATION_BLOCKED`) | Duplicate operation ID, invalid journal state transition, or concurrent modification conflict. |
| `unresolved_intent` | `UNRESOLVED_INTENT` | `4` (`EXIT_MUTATION_BLOCKED`) | Operation remains in intent/dispatched state without terminal outcome recording. |
| `state_locked` | `STATE_LOCKED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | State database (`state.db`) or lock file (`state.db.lock`) is locked by another process. |
| `state_corrupt` | `STATE_CORRUPT` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | SQLite database file is corrupted or failed schema integrity checks. |
| `unsafe_state_dir` | `UNSAFE_STATE_DIR` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | State directory permissions are unsafe (must be `0700` POSIX directory permissions) or owned by wrong user. |
| `lock_unsupported` | `LOCK_UNSUPPORTED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Operating system or filesystem does not support `fcntl.flock` file locking semantics. |

---

### 2.7 Lifecycle & System

| ErrorCode String | Enum Constant | Standard Exit Code | Category Description & Trigger Conditions |
|---|---|---|---|
| `cancelled` | `CANCELLED` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) / `130` (`EXIT_INTERRUPTED`) | Execution explicitly cancelled by policy engine or host caller. |
| `interrupted` | `INTERRUPTED` | `130` (`EXIT_INTERRUPTED`) | Invocation interrupted by POSIX signal (`SIGINT`, `SIGTERM`, `KeyboardInterrupt`). |
| `internal_error` | `INTERNAL_ERROR` | `3` (`EXIT_FATAL_READ_OR_LOCAL`) | Unexpected runtime exception or internal invariant violation in controller core. |

---

## 3. Exception Hierarchy

All custom exceptions inherit from `OctodotError` defined in `octodot.errors`.

```text
OctodotError (Base exception, carries code: ErrorCode and message: str)
├── ValidationError
│   ├── PlanValidationError (Plan schema or validation failure)
│   ├── ResultValidationError (Result schema or validation failure)
│   ├── JsonContractError (JSON parsing/canonicalization violation)
│   └── ReferenceResolutionError (Action reference resolution failure)
├── ExecutionEligibilityError (Plan template disabled or placeholders present)
├── AuthorizationError (Grant or recovery fence verification failure)
├── StateStoreError (SQLite state store locking, corruption, or permission error)
└── TransportFailureError (Sanitized transport failure)
```

### Exception Detail Summary
- `OctodotError`: Root exception for all domain errors. Accepts `ErrorCode` instance or string code. String formatting: `[{self.code}] {self.message}`.
- `ValidationError`: Base exception for data contract and syntax validation failures.
- `PlanValidationError`: Raised during `jules-controller.plan.v1` plan validation.
- `ResultValidationError`: Raised during `jules-controller.result.v1` result validation or output limit overflow.
- `JsonContractError`: Raised when parsing JSON documents violating canonical limits (e.g. nonfinite floats, key ordering).
- `ReferenceResolutionError`: Raised when an action reference selection is invalid or targets an invalid op/selection.
- `ExecutionEligibilityError`: Raised when a plan contains `enabled: false` actions or unedited placeholder strings (`<...>`, `TODO`).
- `AuthorizationError`: Raised on missing, invalid, expired, or revoked grants, or stale recovery fence.
- `StateStoreError`: Raised when SQLite database operations fail due to locking, corruption, filesystem permissions, or disk space.
- `TransportFailureError`: Raised on sanitized network communication or upstream HTTP protocol failures.

---

## 4. Standard Exit Codes, Precedence, and Result Aggregation

### 4.1 Standard Exit Code Definitions

The controller uses six standard integer exit codes:

| Exit Code | Constant | Meaning & Scope |
|---|---|---|
| **`0`** | `EXIT_OK` | **Complete / Success**. All plan actions completed successfully. |
| **`2`** | `EXIT_WAITING` | **Waiting / Yielded**. Async predicate active or polling yield. Resume token (`resume_ref`) generated. |
| **`3`** | `EXIT_FATAL_READ_OR_LOCAL` | **Fatal Read or Local Failure**. Input validation failure, schema violation, local state store error, or read operation failure. |
| **`4`** | `EXIT_MUTATION_BLOCKED` | **Mutation Blocked or Rejected**. Mutation action blocked before dispatch (grant missing/invalid/expired, fence stale, verifier unavailable), rejected, or resulted in `uncertain_effect`. |
| **`5`** | `EXIT_PARTIAL_OR_UNSUPPORTED` | **Partial Coverage or Unsupported Operation**. Read query yielded incomplete pagination / partial coverage, or feature is unsupported. |
| **`130`** | `EXIT_INTERRUPTED` | **Interrupted**. Process interrupted by POSIX signal (`SIGINT`, `SIGTERM`, `KeyboardInterrupt`). |

---

### 4.2 Process Exit Code Precedence

When an execution plan contains multiple actions returning differing exit codes, the process exit code is evaluated by combining action exit codes in `octodot.errors.combine_exit_codes` according to strict precedence:

$$\mathbf{130} > \mathbf{4} > \mathbf{3} > \mathbf{5} > \mathbf{2} > \mathbf{0}$$

- If `130` is present in any action exit code, the process exit code is `130`.
- Otherwise, if `4` is present, the process exit code is `4`.
- Otherwise, if `3` is present, the process exit code is `3`.
- Otherwise, if `5` is present, the process exit code is `5`.
- Otherwise, if `2` is present, the process exit code is `2`.
- Otherwise (or if action set is empty), the process exit code is `0`.

```python
def combine_exit_codes(codes: Iterable[int]) -> int:
    """Combine exit codes according to precedence: 130 > 4 > 3 > 5 > 2 > 0.

    If codes is empty, returns EXIT_OK (0).
    """
```

---

### 4.3 Overall Result Status Derivation Rules

Process exit code combination and overall result status derivation (`ResultBuilder.build()`) are **separate aggregation rules**. The overall `status` field in `jules-controller.result.v1` is evaluated independently by inspecting the set of individual `ActionResult.status` values according to the following priority cascade:

1. `interrupted`: if `ActionResultStatus.INTERRUPTED` is present.
2. `error`: if `ActionResultStatus.ERROR` is present.
3. `blocked`: if `ActionResultStatus.BLOCKED` is present.
4. `rejected`: if `ActionResultStatus.REJECTED` is present.
5. `unknown`: if `ActionResultStatus.UNKNOWN` is present.
6. `unsupported`: if `ActionResultStatus.UNSUPPORTED` is present.
7. `partial`: if `ActionResultStatus.PARTIAL` is present.
8. `waiting`: if `ActionResultStatus.WAITING` is present.
9. `skipped`: if all actions are `ActionResultStatus.SKIPPED` (and actions list is non-empty).
10. `ok`: otherwise.

### 4.4 Independence of Exit Code vs Overall Status

Because exit code precedence ($130 > 4 > 3 > 5 > 2 > 0$) and overall status derivation (`interrupted` > `error` > `blocked` > `rejected` > `unknown` > ...) operate independently:

- An execution containing both an `error` action (`exit_code = 3`, `status = "error"`) and a `blocked` mutation action (`exit_code = 4`, `status = "blocked"`) produces **`exit_code = 4`** (since $4 > 3$) and **`status = "error"`** (since `error` takes precedence over `blocked` in status derivation).
- An execution containing a `waiting` action (`exit_code = 2`, `status = "waiting"`) and an `unsupported` action (`exit_code = 5`, `status = "unsupported"`) produces **`exit_code = 5`** and **`status = "unsupported"`**.

### 4.5 Aggregation Combination Examples

| Action Result Statuses Present | Action Exit Codes Present | Combined Process Exit Code | Derived Overall Result Status |
|---|---|---|---|
| `["interrupted"]` | `[130]` | `130` | `interrupted` |
| `["error", "blocked"]` | `[3, 4]` | `4` | `error` |
| `["blocked"]` | `[4]` | `4` | `blocked` |
| `["error"]` | `[3]` | `3` | `error` |
| `["unsupported", "waiting"]` | `[5, 2]` | `5` | `unsupported` |
| `["partial"]` | `[5]` | `5` | `partial` |
| `["waiting"]` | `[2]` | `2` | `waiting` |
| `["ok", "ok"]` | `[0, 0]` | `0` | `ok` |
| `[]` (empty actions) | `[]` | `0` | `ok` |
