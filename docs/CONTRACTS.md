# Octodot Protocol and Interface Contracts

This document specifies the frozen executable contracts, runtime schemas, canonical hashing algorithms, operation inventory, error codes, exit codes, and typed port interfaces for the `octodot` core runtime (frozen in slice S01).

---

## 1. Protocol and Envelope Specifications

### 1.1 Runtime Plan Envelope (`jules-controller.plan.v1`)

Runtime plans are immutable JSON documents representing an ordered sequence of named actions. Plans have a closed schema (`additionalProperties: false`) and reject unknown fields, nonfinite numbers, and duplicate keys.

```json
{
  "schema_version": "jules-controller.plan.v1",
  "plan_id": "<string>",
  "plan_hash": "sha256:<64-hex>",
  "profile": "<string>",
  "execution": {
    "mode": "read_only" | "authorized_get" | "mutation"
  },
  "scope": {
    "repository": "OWNER/REPO",
    "branch": "<optional-string>",
    "sessions": ["<optional-string>", ...]
  },
  "limits": {
    "deadline_seconds": 180,
    "request_timeout_seconds": 20,
    "max_http_requests": 120,
    "max_posts": 0,
    "max_pages": 100,
    "max_sessions": 200,
    "max_response_bytes": 8388608,
    "max_total_bytes": 33554432,
    "max_output_bytes": 65536
  },
  "actions": [
    {
      "id": "<action-id>",
      "op": "<op-from-inventory>",
      ...
    }
  ],
  "output": {
    "format": "json" | "summary",
    "destination": "<optional-path>"
  }
}
```

#### Plan Validation Rules
1. **`plan_hash` Integrity**: `plan_hash` must equal the canonical hash of the plan dictionary with the `"plan_hash"` field removed (`compute_plan_hash(plan)`).
2. **Execution Modes**:
   - `read_only`: Only read, local, and diagnostic operations permitted; `limits.max_posts` must equal `0`.
   - `authorized_get`: Same as `read_only` with credential resolution for reads; `limits.max_posts` must equal `0`.
   - `mutation`: Mutation operations permitted; `limits.max_posts` must be at most `1` (and never exceed 1 per mutation action).
3. **Action References**:
   - Read actions may reference **only** an earlier read action's typed selection (e.g. `{"from": "<earlier_id>", "select": "<selection_name>"}`).
   - Forward references, self-references, and references to mutation actions are strictly rejected (`invalid_reference`).
   - The referenced selection name must be present in the target op's `allowed_selections` list.
   - Arbitrary JSONPath (e.g. `$.sessions[0]`) or expressions are strictly rejected (`invalid_reference`).
   - Dynamic mutation targets: Any reference or expression inside a mutation action's `target` or `payload` is strictly rejected (`dynamic_mutation_target`). Mutations must carry literal frozen targets and payloads.
4. **Execution Eligibility Check**:
   - Before accessing credentials or the network, plans undergo `check_execution_eligibility(plan)`.
   - If any mutation action has `enabled: false`, it fails with `template_disabled`.
   - If any string in the plan matches placeholder tokens (e.g. `<...>` such as `<APPROVED_TEXT>`, `<UNISSUED_AUTH_REF>`, or containing `REPLACE_ME`, `TODO`, `CHANGEME`), it fails with `placeholder_present`.

---

### 1.2 Runtime Result Envelope (`jules-controller.result.v1`)

```json
{
  "schema_version": "jules-controller.result.v1",
  "plan_id": "<string>",
  "status": "ok" | "waiting" | "skipped" | "partial" | "unsupported" | "blocked" | "rejected" | "unknown" | "error" | "interrupted",
  "exit_code": 0 | 2 | 3 | 4 | 5 | 130,
  "action_results": [
    {
      "action_id": "<action-id>",
      "op": "<op>",
      "status": "<status>",
      "exit_code": 0 | 2 | 3 | 4 | 5 | 130,
      "error_code": "<optional-error-code>",
      "coverage": { ... },
      "data": { ... }
    }
  ],
  "coverage": {
    "complete": true | false,
    "snapshot_atomic": false,
    "pages": 0,
    "items": 0,
    "skipped_scope": [],
    "reasons": [],
    "resume_ref": null | "<string>"
  },
  "omitted_attention_items": [],
  "resume_ref": null | "<string>",
  "output_bytes": 1234
}
```

#### Result Constraints
- `exit_code` must match the combined precedence of all action exit codes.
- `output_bytes` (and the serialized JSON byte length) must not exceed 64 KiB (65,536 bytes). Oversized results raise `oversized_response`.
- Unknown remote response fields are preserved in `data`, tolerating schema evolution without data loss.

---

## 2. Canonical Encoding and Golden Hash Vectors

### 2.1 Canonical Encoding (`octodot.canon.v1`)

Canonical serialization rules:
1. **Encoding**: UTF-8 bytes without BOM.
2. **Key Ordering**: All dictionary keys sorted lexicographically (`sort_keys=True`).
3. **Compact Separators**: No unnecessary whitespace (`separators=(",", ":")`).
4. **Exact Strings**: Exact character sequences preserved. **No** Unicode normalization (NFC vs NFD preserved as-is) and **no** newline normalization (`\n` vs `\r\n` preserved as-is).
5. **No Nonfinite Values**: `NaN`, `Infinity`, and `-Infinity` are rejected.
6. **Hash Format**: `sha256:<64-hex-characters>`.

### 2.2 Volatile Metadata Exclusion in Context Hashing

Context hashes (`context_hash`) exclude volatile observation timestamps and local scan identifiers:
- `timestamp`, `timestamps`
- `scan_id`, `scan_ids`
- `scanned_at`
- `observed_at`
- `local_time`, `local_timestamp`
- `evidence_time`
- `created_at`
- `read_at`, `fetched_at`

### 2.3 Golden Hash Vectors

| Test Vector | Input Description | Exact Canonical SHA-256 Digest |
|---|---|---|
| **Unicode NFC** | `{"text": "\u00e9"}` | `sha256:42d3cbf59fdccced04e5dff14433fb52d34d58e385e9770ffd896ff517d63b92` |
| **Unicode NFD** | `{"text": "e\u0301"}` | `sha256:9b53287cd41955684903378d2b1b4a3ddea9d80d67dcd026319a7c5a9a8a8b42` |
| **Newline LF** | `{"text": "line1\nline2"}` | `sha256:c485220c7f2b51d3960a5e118cf7181debd086140691e1c96db8ec13e1cd84cf` |
| **Newline CRLF** | `{"text": "line1\r\nline2"}` | `sha256:5d6a0695821a214ea832b81625179b431f1ff375e1e1492aaa06b138b01b0b9d` |
| **Key Order** | `{"b": 2, "a": 1}` | `sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777` |
| **Branch Lower** | `{"branch": "main"}` | `sha256:6461b20cebcb7034bd8b13089d21a90cf5ce300bd7d74eb625c7a342cf6ccdac` |
| **Branch Upper** | `{"branch": "Main"}` | `sha256:18d62a982fab18f31724a7322a216fe23b797202e99a00ca5005fd417f45ba1d` |
| **Context Filtered** | See Test S01-T04 | `sha256:78438ac8a3a932631ccedd5c075bfadacf899b7daafac850bc315bad0f88e1d2` |

---

## 3. Fixed Operation Inventory

The runtime inventory contains exactly 15 operations:

| Operation | Category | Capability | Description | Allowed Selections |
|---|---|---|---|---|
| `inventory.collect` | `read` | `core` | Collect connected sources and sessions | `sessions`, `sources`, `session_names`, `source_names`, `active_session` |
| `session.inspect` | `read` | `core` | Inspect session binding, state, latest plan | `session`, `binding`, `state`, `title`, `latest_plan`, `latest_plan_id`, `latest_plan_hash`, `feedback_bundle` |
| `chats.collect` | `read` | `core` | Collect conversation activities and bundle | `messages`, `activities`, `candidate_bundle`, `latest_activity_id`, `feedback_bundle`, `last_message` |
| `chats.reply` | `mutation` | `core` | Single approved reply message | *(none)* |
| `tasks.create` | `mutation` | `core` | Create session with `requirePlanApproval=True` | *(none)* |
| `plans.approve` | `mutation` | `core` | Approve reviewed plan on waiting session | *(none)* |
| `suggestions.collect`| `read` | `unsupported_public_api` | Public API does not provide suggestions resource | `suggestions`, `items` |
| `artifacts.export_patch` | `local` | `core` | Inert export of patch artifacts | `patch`, `manifest`, `artifact_id` |
| `publication.verify` | `read` | `core` | Read-only verification of GitHub publication | `verified`, `details`, `publication_state` |
| `operations.reconcile`| `local` | `core` | Read-only reconciliation of mutation state | `reconciled_state`, `operation_record` |
| `events.read` | `read` | `core` | Read durable events from outbox | `events`, `event_ids` |
| `events.ack` | `local` | `core` | Acknowledge delivered events | `acked_event_ids`, `status` |
| `wait` | `local` | `core` | Bounded resumable wait for predicates | `resumed`, `predicate_matched`, `job_id` |
| `capabilities.inspect`| `diagnostic` | `core` | Inspect profile capabilities and evidence | `capabilities` |
| `healthcheck` | `diagnostic` | `core` | Check controller health and store accessibility | `healthy`, `status` |

---

## 4. Error Codes and Exceptions

### 4.1 Closed ErrorCode Enumeration
- **Input & Syntax**: `invalid_input`, `duplicate_key`, `nonfinite_number`, `unknown_field`, `oversized_input`, `invalid_reference`, `dynamic_mutation_target`, `placeholder_present`, `template_disabled`, `schema_too_new`
- **Limitations**: `unsupported_public_api`, `unsupported_exact_commit`, `unsupported_atomic_plan_approval`
- **Authorization**: `auth_denied`, `grant_missing`, `grant_invalid`, `grant_expired`, `grant_revoked`, `verifier_unavailable`, `recovery_fence_stale`
- **Transport**: `rate_limited`, `transport_error`, `timeout`, `uncertain_effect`, `malformed_response`, `oversized_response`, `budget_exhausted`
- **Identity & Scope**: `partial_coverage`, `identity_ambiguous`, `binding_mismatch`, `branch_unverified`, `unknown_state`, `accepted_identity_unverified`
- **State & Journal**: `operation_conflict`, `unresolved_intent`, `state_locked`, `state_corrupt`, `unsafe_state_dir`, `lock_unsupported`
- **Lifecycle**: `cancelled`, `interrupted`, `internal_error`

### 4.2 Exception Hierarchy
- `OctodotError(code, message)`: Root base exception.
  - Never includes raw HTTP response bodies, bearer tokens, or secrets in messages.
  - Subclasses: `ValidationError`, `PlanValidationError`, `ResultValidationError`, `JsonContractError`, `ReferenceResolutionError`, `ExecutionEligibilityError`, `AuthorizationError`, `StateStoreError`, `TransportFailureError`.

---

## 5. Exit Codes and Precedence

Exit codes:
- `0` (`EXIT_OK`): Complete
- `2` (`EXIT_WAITING`): Waiting / yielded with resume ref
- `3` (`EXIT_FATAL_READ_OR_LOCAL`): Invalid input or fatal read / local failure
- `4` (`EXIT_MUTATION_BLOCKED`): Blocked / rejected / unknown mutation
- `5` (`EXIT_PARTIAL_OR_UNSUPPORTED`): Partial coverage or unsupported capability
- `130` (`EXIT_INTERRUPTED`): Interrupted (SIGINT/SIGTERM)

### Precedence Rule
When multiple actions yield differing exit codes, they are combined with strict precedence:
$$130 > 4 > 3 > 5 > 2 > 0$$

Function: `combine_exit_codes(codes: Iterable[int]) -> int`

---

## 6. Typed Port Interfaces (`typing.Protocol`)

Parallel slices build on these frozen abstract protocols:

1. **`Clock`**: Deterministic fake-able clock providing `now_utc() -> datetime` and `sleep(seconds: float)`.
2. **`CredentialSource`**: Lazy credential loader with observable spy methods (`was_accessed()`, `access_count()`).
3. **`Transport`**: Fixed-origin HTTP wrapper returning sanitized `TransportOutcome` with byte/request counts and `uncertain_effect`.
4. **`DispatchTicket` & `TicketAuthority`**:
   - `DispatchTicket`: Opaque, journal-minted, single-use ticket carrying `ticket_id`, `operation_id`, `request_hash`, and an opaque journal-generated `nonce`.
   - `TicketAuthority` Protocol: `redeem(ticket: DispatchTicket, request_hash: str) -> bool`. Atomically validates that the ticket was issued by the journal for that exact `operation_id` + `request_hash` and marks it consumed (second redeem returns `False`).
5. **`JulesReadAPI`**:
   - Typed Jules API wrapper for `sources`, `sessions`, `activities`.
   - The `JulesReadAPI` implementation is constructed with a `TicketAuthority` supplied by the journal and must call `redeem(ticket, request_hash)` with `request_hash` computed by `contracts.request_hash` over the exact outgoing request body/target before any transport call; a failed redeem means zero transport attempts.
   - Internal mutation methods (`sessions_create`, `sessions_send_message`, `sessions_approve_plan`) require a `DispatchTicket` and return a frozen `MutationResponse(outcome: TransportOutcome, session: SessionRecord | None = None)`. These methods never retry, never raise for transport-level failures (they return outcome with `uncertain_effect=True` for timeout/disconnect/5xx/malformed-or-oversized success, and clear 4xx rejection with `uncertain_effect=False`), and raise only for local pre-dispatch validation failures (zero attempts).
6. **`Store`**:
   - Durable SQLite storage with short transactions, owner locking (`acquire_lock`, `release_lock`), operations, events, and receipts.
   - *Note*: The `Store` Protocol in `contracts.py` specifies the minimum interface surface. Slice S03's concrete store implementation may add specialized storage and query methods (scans, checkpoints, jobs, action_results, authorization_records, manifests) that later slices consume.
7. **`RecoveryFence` / `ProfileEpochSource`**: External host-controlled configuration epoch outside worker-writable database.
8. **`ReadService`**: Bounded reader providing `collect(scope, limits)`, `inspect(binding)`, and `chats(selection)`.
9. **`GrantVerifier`**: Trusted grant verification returning `VerifiedGrant` or `GrantBlocker`.
10. **`MutationJournal`**: Single-attempt mutation journal managing `OperationRecord`, `begin_dispatch -> DispatchTicket`, and outcome recording.
11. **`ActionHandler`**: Named action executor returning `ActionResult`.
12. **`Receiver`**: Durable event receiver with distinct receiver acceptance and channel delivery stages.
13. **`Provider`**: Optional external or UI provider reporting capability and provenance.

---

## 7. Compatibility and Versioning Statement

- **Encoding Version**: `octodot.canon.v1`. Any change to key sorting, separator conventions, or character encoding constitutes a breaking version increment.
- **Plan Schema Version**: `jules-controller.plan.v1`. Future versions require distinct schema version identifiers; controllers encountering newer versions fail closed with `schema_too_new`.
- **Result Schema Version**: `jules-controller.result.v1`.
- **Implicit Namespace Package**: In slice S01 through S08, `src/octodot` is an implicit namespace package without `__init__.py`. Slice S09 adds `src/octodot/__init__.py`.

---

## 8. Stated Unsupported Items

The following features are explicitly unsupported by this release and fail closed:
1. **Exact-Commit Pinning**: The underlying API binds sessions to branches, not commit SHAs. Plans requesting commit pinning fail with `unsupported_exact_commit`.
2. **Atomic Exact-Plan Approval**: The underlying API's `approvePlan` targets a session without a plan-version precondition. Atomicity cannot be guaranteed across server races; reported as `unsupported_atomic_plan_approval`.
3. **Suggestions Public API**: No public suggestions endpoint is available in the checked Jules API inventory. `suggestions.collect` in API mode is classified as `unsupported_public_api`.
