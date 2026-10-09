# Architecture Overview

## 1. Overview

`octodot` is an offline-first, portable, single-attempt controller for the Google Jules API. Its primary purpose is to execute structured plans reliably while providing durable event sourcing and reconciliation. It guarantees strict authorization and ensures that mutations are executed safely, without implicit or unverified retries, preserving the fail-closed security invariant.

## 2. Architecture & Data Flow

The controller's runtime pipeline follows an ordered execution model that ensures safety and determinism:

1. **Plan Formulation**: A structured JSON plan (`jules-controller.plan.v1`) is provided to the runner.
2. **Validation**: The entire plan undergoes structural validation, checking for unknown fields, valid types, and resolving variables.
3. **ReadService**: Bounded full or filtered scans securely fetch inventory and session state without risking writes or side effects.
4. **Journal**: Intent for any mutation is durably recorded before execution. A single-use dispatch ticket is minted to bind the exact payload and prevent duplicates.
5. **Dispatch**: The execution phase triggers network transport using standard-library implementations. Exactly one POST attempt is made per ticket.
6. **Reconciler**: If a dispatch fails cleanly, or if uncertainty persists (e.g., timeout), the mutation drops to a read-only reconciliation phase. The controller verifies effects securely rather than blindly retrying.

## 3. Core Subsystems

### Storage Layer
The state of `octodot` is backed by durable, single-file SQLite event sourcing. It handles operation state transitions via Compare-and-Swap (CAS) strategies to avoid lost updates. A host-controlled **recovery fence** provides strict configuration epochs, safely resolving state inconsistencies across snapshot rollbacks or corrupted DB files.

### Mutation Safety
Mutations are fundamentally gated behind single-attempt limits and explicit intent tracking:
- **Dispatch tickets** bind requests uniquely, prohibiting unauthorized replication.
- **Single-attempt journal**: State transitions to `dispatching` *before* the network call, resolving uncertain outcomes to `UNKNOWN` on crash.
- **Zero unverified retries**: Implicit retries on `POST` are strictly forbidden. Timeouts or ambiguous responses must be resolved via the read-only Reconciler.

### Authorization
`octodot` employs a fail-closed architecture:
- **DisabledGrantVerifier**: Included as the default verifier to fail securely when no external, trusted adapter is provided.
- Automated mutability features depend entirely on verified cryptographic grants bound to explicit payloads. Local write access to plan files confers zero authority.

### Read & Wait Services
- **Bounded Resource Collection**: API pagination limits are strictly enforced, averting memory overruns and excessive costs.
- **Ambiguity Scanning**: Session contexts are rigorously checked to handle asynchronous server updates correctly.
- **Resumable Polling**: Waiting operations are fully decoupled from process lifecycles and safely tested via fake clock determinism.

## 4. Testing Philosophy

The testing framework strictly asserts offline operations and verifies the absence of accidental credentials leakage or actual network traversal.

- **Offline-Only CI**: Unit and integration testing occurs fully offline to ensure robust test fidelity and security.
- **Zero Live Network**: Test suites prohibit live connections and verify this restriction directly using an immutable execution wrapper (`FakeGrantVerifier`, bounded assertions).
- **Standard Library Only**: Operations are fully validated natively in Python 3.10+ using only standard libraries. No third-party networking or test dependencies exist in the main runtime module.
