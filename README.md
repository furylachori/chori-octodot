# chori-octodot

A planning-only repository for a portable Jules controller: read task inventory and conversations, persist reliable events, and execute narrowly authorized actions with durable uncertainty handling.

## Start here

- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): 22 independently scoped slices, dependency waves, tests and release gates
- [Machine-readable plan](plan/implementation-plan.json): slice ownership, interfaces, dependency edges, permissions and stopping conditions
- [Planning schema](plan/implementation-plan.schema.json): validates the implementation-planning artifact; it is not a runtime execution schema

## Status

No controller implementation is included yet. The historical relay reported 28 passing offline tests; they were inspected but not rerun for this plan. The first implementation slice must reproduce that baseline. No live API writes or interoperability checks were performed by this planning work.

The delivery sequence is offline contracts and tests → live read-only parity → one explicitly approved reply round trip → optional, separately approved task creation and plan approval. Publishing this plan does not authorize any of those actions.

The proposed runtime uses Python 3.10+, typed API wrappers, an ordered JSON runner and private durable SQLite state. Suggested Tasks API support is explicitly unavailable in the checked public contract; optional UI/import readers stay separate. Timestamp filtering is an optional capability-tested optimization with full-scan fallback.

The controller will not apply code, push, merge, deploy or create coding tasks without the corresponding explicit authority. Runtime state, credentials and private conversation evidence never belong in this repository.

## Implementation handoff

Assign a slice only after its listed dependencies and entry gates are accepted. Keep edits within its file scope, run its finite tests and stop on unresolved safety or authorization blockers. The full plan defines independent reviewer/challenger checks without an open-ended review loop.

No license has been selected. A license file and license metadata are intentionally absent pending the owner's choice.
