# Octodot Trust and Authorization Architecture

This document defines the security architecture, threat model, trust boundaries, and authorization protocols for the `octodot` runtime.

---

## 1. Executive Summary & Core Principle

**The Fail-Closed Invariant**: In the absence of an externally injected, trusted grant authority, all automated mutation operations (session creation, reply messages, plan approvals) are strictly disabled (`verifier_unavailable`).

No local file, environment variable, user assertion, git commit status, repository ownership, or code review badge on the worker machine confers authority to execute mutations.

---

## 2. Threat Model & Trust Boundary

### 2.1 The Untrusted Worker Environment

In the `octodot` execution model:
- The runner / worker process runs in an environment where files on the local filesystem may be modified by untrusted agents, build scripts, or code generation processes.
- An attacker or misbehaving agent is assumed to have write access to the workspace:
  - Can modify plan files (`plan.json`) and set `"enabled": true`.
  - Can invent arbitrary `"authorization_ref"` values.
  - Can inject `"approved": true` in local plan preconditions, payloads, or scratch files.
  - Can create fake receipt or assertion files on disk claiming that human or automated approval occurred.
  - Can inspect repository contents and local configuration.

### 2.2 The Trust Boundary

```
+-------------------------------------------------------------------------+
|                       COORDINATOR / HOST ENVIRONMENT                    |
|                                                                         |
|  - Real API Credentials (X-Goog-Api-Key) held lazily in CredentialSource|
|  - Recovery Fence / Profile Epoch Source (outside worker-writable state)|
|  - External Trusted Grant Authority (Coordinator approval workflow)    |
+-------------------------------------------------------------------------+
                                     |
                         [Host-Injected Adapter Port]
                                     |
                                     v
+-------------------------------------------------------------------------+
|                  RUNNER / WORKER ENVIRONMENT (Same OS)                  |
|                                                                         |
|  - Executes ReadService full/filtered scans                             |
|  - Prepares PreparedAction records (pure canonical hashing)             |
|  - Submits authorization_ref + PreparedAction to GrantVerifier          |
|  - Default Verifier: DisabledGrantVerifier (Fails closed)               |
|  - Stores durable operation journal & event outbox in SQLite            |
+-------------------------------------------------------------------------+
```

### 2.3 Assertions as Audit Evidence Only

**Rule**: *Same-OS writable assertions are audit evidence only, never authorization authority.*

A local file stating:
```json
{
  "approved": true,
  "approver": "maintainer@example.com"
}
```
is treated solely as unverified audit telemetry. The runner **never** parses local assertion files to grant mutation permissions. Only a grant returned by the trusted `GrantVerifier` port bound to the coordinator environment constitutes authority.

---

## 3. The Grant Binding Contract

A `VerifiedGrant` authorizes **at most one** mutation dispatch attempt (`max_attempts == 1`).

To prevent replay, scope creep, or confused-deputy attacks, the grant cryptographically and deterministically binds to all of the following parameters:

| Binding Field | Constraint | Failure Code |
|---|---|---|
| `profile` | Exact profile name string match | `grant_invalid` |
| `profile_epoch` | Must match host-controlled recovery fence epoch | `recovery_fence_stale` |
| `source` | Exact resource name of connected source | `grant_invalid` |
| `repository` | Exact `OWNER/REPO` case-sensitive string | `grant_invalid` |
| `branch` | Exact case-sensitive starting branch name | `grant_invalid` |
| `session` | Exact session resource name (`sessions/...` or null for create) | `grant_invalid` |
| `action` | Exact plan action ID | `grant_invalid` |
| `operation_id` | Exact idempotency operation identifier | `grant_invalid` |
| `payload_hash` | Canonical SHA-256 digest of exact mutation payload (preserving exact Unicode and newlines) | `grant_invalid` |
| `context_hash` | Canonical SHA-256 digest of observed conversation/session context (excluding volatile observation timestamps) | `grant_invalid` |
| `plan_hash` | Canonical SHA-256 digest of the entire plan document | `grant_invalid` |
| `publication_scope`| Target publication effect (e.g. `none`) | `grant_invalid` |
| `authorizing_source`| Identity/origin of the external authority | `grant_invalid` |
| `expiry` | ISO8601 UTC timestamp; evaluated against injected clock | `grant_expired` |
| `max_attempts` | Must be exactly 1 | `grant_invalid` |

Any mismatch between the grant and the `PreparedAction` immediately produces a typed `GrantBlocker` and terminates mutation dispatch before any transport call.

---

## 4. Recovery Fence and Credential Rotation

### 4.1 Host-Controlled Configuration Epoch

The recovery fence (`RecoveryFence` / `ProfileEpochSource`) maintains a strictly monotonic generation/epoch counter for each profile:
1. The epoch counter is stored **outside** the worker-writable database directory.
2. Any configuration change—such as rotating synthetic credentials, re-authenticating, or switching coordinator profiles—advances the epoch counter.
3. Neither credential values nor secret keys/key fingerprints are ever stored in the durable SQLite database.

### 4.2 Invalidation on Epoch Advance

When the host advances the epoch from $E$ to $E+1$:
- All previously issued grants carrying epoch $E$ become immediately invalid (`recovery_fence_stale`).
- Prior session and repository bindings are invalidated for mutation dispatch.
- Resumption of mutations is disabled until:
  1. Fresh read-only identity verification is completed under epoch $E+1$.
  2. A new grant referencing epoch $E+1$ is issued by the external authority.

---

## 5. Verifier Implementations

### 5.1 `DisabledGrantVerifier` (Default)
Ships as the default in production and standalone execution. Always returns:
```python
GrantBlocker(code=ErrorCode.VERIFIER_UNAVAILABLE, reason="...")
```
Ensures that out-of-the-box runs are safe and cannot execute remote mutations without explicit host adapter integration.

### 5.2 `HostGrantVerifierAdapter` (Production Port)
An adapter wrapping an externally supplied trusted verifier callable or service. Injected by the host coordinator environment. The runner never instantiates this from local workspace files.

### 5.3 `FakeGrantVerifier` (Offline Test Fixture)
An in-memory fake used exclusively in offline test suites:
- Marked fixture-only via an immutable class-level `FIXTURE_ONLY = True` marker.
- Protected against mutation (`__setattr__` guard prevents altering the marker on instances).
- Never touches real credentials or environment variables (`GOOGLE_JULES_KEY`).
- Never exports approvals to disk.

### 5.4 Composition Guard and In-Process Security Limits

**Core Security Notice**: *In-process Python object state and environment variables on the worker machine are NOT a security boundary.*
Because worker-writable code or scripts execute in the same Python runtime, in-process flags or environment variables are inherently mutable by a sufficiently determined worker.

The module-level `require_verifier_allowed(verifier, *, live: bool)` guard enforces composition hygiene:
- When `live=True`, any verifier marked `FIXTURE_ONLY` raises `OctodotError(ErrorCode.AUTH_DENIED)`.
- When `live=True`, only `HostGrantVerifierAdapter` or `DisabledGrantVerifier` are permitted.
- `DisabledGrantVerifier` is allowed in live mode, but its `verify()` method always fails closed with `VERIFIER_UNAVAILABLE`.

This composition barrier prevents accidental wiring of test fakes into live transports. The **actual, immutable trust boundary** remains the external host coordinator and its injected verifier running outside the untrusted worker environment.

---

## 6. Prohibited Authority Inferences

The runtime explicitly rejects the following inferences:
1. **Repository Ownership**: Being the owner or committer of the target GitHub repository does not confer Jules mutation permission.
2. **Code Review Authority**: PR approvals, LGTM comments, or branch protection rules in GitHub do not substitute for a Jules controller grant.
3. **Read Access**: Having read access to sessions or sources does not imply permission to send messages or approve plans.
4. **Local Configuration**: Setting environment variables or flags on the worker machine cannot elevate an unverified grant into a verified grant.
