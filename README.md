# chori-octodot

A repository containing the implementation plan and offline core controller on the impl branch for a portable Jules controller: read task inventory and conversations, persist reliable events, and execute narrowly authorized actions with durable uncertainty handling.

## Start here

- [Operations Guide](docs/OPERATIONS.md): Installation, running, state directory placement, exit codes, and operational limits
- [Release Checklist](docs/RELEASE_CHECKLIST.md): Gate G2 offline verification checklist and exact test commands
- [Contracts and Schemas](docs/CONTRACTS.md): Execution plans, result formats, and typed interfaces
- [Authorization Architecture](docs/AUTHORIZATION.md): Grant verification, recovery fencing, and execution boundaries
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): 22 independently scoped slices, dependency waves, tests and release gates
- [Machine-readable plan](plan/implementation-plan.json): Slice ownership, interfaces, dependency edges, permissions and stopping conditions
- [Planning schema](plan/implementation-plan.schema.json): validates the implementation-planning artifact; it is not a runtime execution schema

## Status

The offline core controller (slices S00–S14) is implemented and verified on the `impl/octodot-core` branch. It provides strict typed contracts, durable SQLite state (operation journal, events, checkpoints), a static handler registry, and read-only compatibility shorthands.

The delivery sequence begins with offline contracts and tests (completed on the impl branch) → live read-only parity → one explicitly approved reply round trip → optional, separately approved task creation and plan approval. Publishing this repository does not authorize any live actions.

Live verification gates G3–G6 have not been run. Live mutations remain strictly disabled by default via `DisabledGrantVerifier`. Live writes require an external host-provided grant verifier (`HostGrantVerifierAdapter`), and none ships enabled. No live API writes or network access have been performed.

The runtime uses Python 3.10+ standard library only, typed API wrappers, an ordered JSON runner and private durable SQLite state. Suggested Tasks API support is explicitly unavailable in the checked public contract; optional UI/import readers stay separate. Timestamp filtering is an optional capability-tested optimization with full-scan fallback.

The controller will not apply code, push, merge, deploy or create coding tasks without the corresponding explicit authority. Runtime state, credentials and private conversation evidence never belong in this repository.

## Implementation handoff

Assign a slice only after its listed dependencies and entry gates are accepted. Keep edits within its file scope, run its finite tests and stop on unresolved safety or authorization blockers. The full plan defines independent reviewer/challenger checks without an open-ended review loop.

No license has been selected. A license file and license metadata are intentionally absent pending the owner's choice.

