# chori-octodot implementation plan

Plan version 1.0.0 · 7 October 2026 · Planning only

Build a portable Jules controller by extending the existing relay in small, independently testable slices. Ship full-scan read inventory first, then durable observation and offline mutation safety, then prove one explicitly approved reply through the actual UI/API/channel path. Task creation and plan approval have separate optional live gates.

This repository contains planning documents, not an executable controller. Reading, publishing or accepting this plan does not authorize implementation, live API writes, creation of coding tasks, merging or deployment. Future work requires an explicit assignment in an authorized environment. No implementation, original test rerun or live acceptance was performed to produce this plan.

The machine-readable source of slice IDs, dependencies, file ownership and gates is [implementation-plan.json](../plan/implementation-plan.json), with its [planning schema](../plan/implementation-plan.schema.json). This planning format is not a controller execution plan. The runtime protocols to implement remain `jules-controller.plan.v1` and `jules-controller.result.v1`.

## Baseline and current evidence

The inspected relay has a Python 3.10+ standard-library CLI, SQLite state, an exclusive POSIX owner lock, full activity scans, an acknowledged JSONL outbox and a conservative single-attempt reply path. It already separates API acceptance from task completion and protects unknown sends across restart. Its existing authorization flags are caller assertions, not an independent grant boundary.

The historical report says 28 offline tests passed, and static inspection found 28 test methods. They were not rerun during this planning pass. S00 must run the original suite unchanged against its original snapshot privately, then rerun a public-safe mirror with only identifying fixture labels replaced. Preserve every assertion and record those replacements. Do not copy private runtime records or historical documentation into the public repository.

A historical successful authenticated read was reported elsewhere, but it is not current acceptance evidence for this package. GET connectivity, UI session correspondence, actual event delivery, reply round trip, task creation and approval must each be verified at the implementation revision. The original prototype is an input to S00 and is not included here.

## Selected architecture and constraints

- Use a portable Python 3.10+ CLI with typed wrappers over standard-library HTTPS and SQLite. Linux/macOS with a local filesystem supporting POSIX locking are the initial target; Windows support is not claimed.
- The coordinator chooses scope, exact text and permission. An execution worker runs an immutable JSON plan unchanged. The controller handles bounded pagination, polling, persistence and evidence. No model decides each HTTP call.
- Runtime plans are ordered named actions. Only a typed selection from an earlier read action may be referenced. Mutations use literal frozen targets/payloads. There is no general DAG language, expression engine, arbitrary URL or shell action. The DAG below schedules implementation work only.
- Keep complete scans as the correctness baseline. `createTime` optimization is optional, per-profile capability-tested, overlap-based and backed by periodic full reconciliation. A server timestamp cursor is not exactly-once delivery.
- One private durable SQLite database per profile retains operation/event identities. Hold a global workflow owner lock, release it between wait iterations, and never hold a database transaction across HTTP or sleep. Missing or restored stale state disables mutation resumption until a trusted external recovery fence is checked.
- All mutations go through one journal and a trusted grant verifier. A worker-writable assertion, `approved:true` or local signing key is insufficient. Without a real coordinator-controlled verification boundary, automated writes remain disabled.
- Suggested Tasks API mode returns `unsupported_public_api`. Optional UI/import providers must remain read-only and provenance-labelled. API-only operation stays useful.
- Patch export is inert. GitHub publication verification is read-only. Applying patches, direct application changes, pushes, merges, deployment, source connection, deleting sessions and automatic publication are outside this release.
- No license has been selected. Do not add a license file, SPDX declaration or license metadata until the owner chooses one.

### Public API basis

The [REST inventory](https://developers.google.com/jules/api/reference/rest), [session reference](https://jules.google/docs/api/reference/sessions/) and [source reference](https://jules.google/docs/api/reference/sources/) are the basis for the fixed allowlist. Sources are already-connected repositories. Session listing is paginated, so repository/state selection stays local. The controller uses source names returned by the API, not a name invented from `OWNER/REPO`.

The [activity reference](https://jules.google/docs/api/reference/activities/) and [filter announcement](https://jules.google/docs/changelog/2026-01-26-4) document `createTime`. Ordering, boundary inclusivity and late visibility are not treated as correctness guarantees; capability tests cannot create undocumented guarantees.

The [send method](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/sendMessage) and [approve method](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/approvePlan) have empty success responses. Approval targets a session rather than an atomic plan-version precondition. The session reference describes `requirePlanApproval` and `AUTO_CREATE_PR`; the planned default sets approval required and omits `automationMode`. There is no invented `NONE` value or draft-PR flag.

No public suggestions resource appears in the checked inventory. The [Suggested Tasks guide](https://jules.google/docs/suggested-tasks/) describes the product UI. That absence is a capability limit of this plan, not a claim that no future API can exist. Recheck official documentation before expanding any allowlist.

## Delivery order and parallel work

Each slice has one file owner. A contributor may read accepted dependencies but edits only its allowed files. New files under a listed directory are permitted; unrelated directories are not. If a shared contract needs changing, stop and request a scoped contract update before continuing. Never run concurrent writers on the same files.

A slice starts only after its exact `depends_on` slices and entry gates pass. Waves show the earliest useful parallel groups, not a requirement to wait for unrelated optional work. Independent worker branches may proceed together; an authorized maintainer integrates accepted prerequisites before dependent work begins. This plan does not authorize merges.

| Wave | Slices | Outcome |
|---|---|---|
| 0 | S00 | Reproduced and sanitized baseline |
| 1 | S01 | Frozen contracts and interfaces |
| 2 | S02, S03, S04, S05 | Typed API, durable store, pure projections, grant preparation |
| 3 | S06, S08 | Full read service and single-attempt journal |
| 4 | S07, S10, S11, S12; optional S19, S20, S21 | Wait/events, offline mutation handlers, optional read providers |
| 5 | S09; optional S13 | Ordered CLI runner; optional incremental optimization |
| 6 | S14 | Integrated offline core and bounded independent review |
| 7 | S15 | Live read-only UI/API parity |
| 8 | S16 | Explicitly approved reply round trip |
| 9 | Optional S17 | Separately approved task creation |
| 10 | Optional S18 | Separately approved plan execution |

Core dependency edges:

- S00 → S01 → S02, S03, S04, S05
- S02 + S03 + S04 → S06
- S02 + S03 + S05 → S08
- S06 → S07; S06 + S08 → S10, S11, S12
- S07 + S08 → S09
- S09 + S10 + S11 + S12 → S14 → S15 → S16 → optional S17 → optional S18
- Optional branches: S07 → S13; S06 → S19, S20, S21

The first core implementation assignment should cover S00–S14 only, with all live capabilities disabled. S13 and S19–S21 may be deferred without blocking the core. This is a recommendation for a future assignment, not permission to start one. Estimates in each slice are engineering effort, not elapsed-time promises; the JSON gives the same ranges.

## Interface contract to freeze in S01

The proposed package is `src/octodot/`. `contracts.py`, `models.py` and `errors.py` define immutable records and typed ports before parallel implementation.

| Interface | Responsibilities |
|---|---|
| Transport and JulesReadAPI | Fixed origin, validated paths, typed reads, budgets, sanitized errors; internal mutation methods require a journal ticket |
| Store | Migrations, short transactions, owner lock, durable observations/checkpoints/jobs/receipts and recovery fence |
| ReadService | Full inventory, exact session inspection, complete activity history, conservative attention projections |
| GrantVerifier and preparation | Canonical request/context hashes and verification of external authority for one exact action |
| MutationJournal | Immutable operation identity, committed dispatch intent, one local attempt, uncertain-effect reconciliation |
| ActionHandler | One typed named operation producing a bounded ActionResult |
| Receiver | Durable event acceptance, independent actual-channel receipt and idempotent ACK |
| Optional Provider | Explicit capability, identity, provenance and coverage for suggestions, patches or GitHub reads |

The plan envelope includes schema version, immutable plan ID/hash, profile, execution, scope, limits, actions and output. A mutation adds literal target, preconditions, operation ID and authorization reference. Runtime inputs use closed strict schemas; remote response parsing tolerates unknown fields and preserves unfamiliar states. Duplicate JSON keys, NaN/Infinity and oversized input are rejected before execution.

Canonical hashes use a versioned UTF-8 JSON encoding with sorted keys and compact separators. Preserve exact Unicode/text/newlines. The binding hash includes profile, canonical source, repository, exact starting branch and session when present. Context covers binding, relevant remote state and selected activity/plan IDs and content hashes; exclude volatile local timestamps and scan IDs. Only complete unambiguous observations can authorize a write-eligible context.

### Operations and identity

The fixed operation inventory is `inventory.collect`, `session.inspect`, `chats.collect`, `chats.reply`, `tasks.create`, `plans.approve`, `suggestions.collect`, `artifacts.export_patch`, `publication.verify`, `operations.reconcile`, `events.read`, `events.ack`, `wait`, `capabilities.inspect` and `healthcheck`. Shorthands compile to the same runner. Diagnostic endpoints are read-only.

Repository read scope without a branch includes all starting branches in that repository. Writes always bind an explicit case-sensitive starting branch. Absent branch metadata means `branch_unverified`, not permission to use the default. A separately authorized GitHub branch read may supply missing affirmative evidence. Starting branch, exact starting commit, output branch and PR base/head are distinct; this API does not provide commit-pinned creation. Switching profile or credential configuration must advance a host-controlled non-secret epoch, invalidate previous bindings and grant eligibility, and require fresh identity revalidation before writes. Do not store the credential or a fingerprint to detect changes.

### Compatibility

The legacy commands `discover`, `attach`, `poll`, `send`, `events`, `ack`, `status`, `reconcile` and `wait` need golden tests. The private original snapshot remains a frozen oracle. S14 converts the public entry point to a facade over the new runner/journal so it cannot bypass authorization. Preserve offline behavior, output contracts and explicit default-branch selection rules. Requiring trusted grants for legacy live writes is an intentional security tightening and must be documented, not silently hidden as compatibility.

## Gate permissions and acceptance

No gate status is currently passed by this repository. Public gate summaries contain only sanitized evidence. Exact targets, message text, grants, receipts and UI captures remain in an authorized private evidence store.

| Gate | Prerequisite | Exact acceptance | Permission |
|---|---|---|---|
| G0 | S00 | Original 28 tests reproduced privately; public-safe mirror and provenance | Explicit implementation/import assignment |
| G1 | S01 | Strict schemas, type ports, canonical vectors and examples frozen | Same assigned offline scope |
| G2 | S14 | Baseline plus full core offline suites at final revision; reviewer and challenger resolved | Offline only |
| G3 | G2, S15 | Existing UI session and selected activities match full GET evidence; zero POST | Existing authorized reads and UI access |
| G3-F | S13 and G3 read procedure | Optional full/filtered comparison and fallback verified for one profile | GET only; failed optimization does not fail core |
| G4 | G3, S16 | Actual event/receiver/channel plus one approved reply observed in the same UI | Exact reply target/text/context grant and channel authority |
| G5 | G4, S17 | Optional one bounded task created and exact source/branch verified | Separate exact task/source/branch grant; approval required; no automatic publication |
| G6 | G5, S18 | Optional reviewed plan approval observed with known race limitation | Separate latest plan/execution scope grant |

Before a live mutation gate passes, the corresponding normal capability remains disabled. Once its prerequisite gate passes, a specifically issued acceptance grant permits only that one exact gate invocation through the same verifier, recovery fence and journal with max_posts=1. G4 follows G3, G5 follows G4, and G6 follows G5. The acceptance grant is not a general bypass and cannot be replayed or converted into ordinary access; a passing gate also never replaces future current grants.

A G4 grant is not a G5 or G6 grant. Read access, ownership or permission to post code-review findings does not grant task creation, plan execution or code publication. New persistent credential access still requires its own permission. A real trusted verifier and recovery fence are prerequisites to every live mutation.

A harmless reply gate must establish the complete chain: existing UI question → corresponding API activity → durable event → actual receiver acceptance → authorized channel receipt → one exact approved reply POST → new matching activity → the same UI conversation. No stdout-only shortcut, fabricated receiver receipt or induced task failure can pass the gate.

Live gates use one bounded invocation of up to 180 seconds, 120 requests, 100 pages, 200 sessions, 8 MiB per response and 32 MiB total; request timeout is at most 20 seconds and returned summary at most 64 KiB. GET gates permit zero POST. Each specifically granted mutation permits at most one POST. Reaching a cap returns incomplete/waiting evidence and a durable continuation. It never grants another write. Cooperative DNS/already-blocked-I/O overruns remain a stated limitation.

## Failure and recovery behavior

The durable mutation path is `prepared → dispatching → accepted → effect_observed`, with alternatives `blocked_before_dispatch`, `rejected`, `unknown` and `cancelled_before_dispatch`. Commit `dispatching` before transport. Restart recovers an abandoned dispatch as unknown, including a crash that might have occurred before transmission. This promises at most one local dispatch attempt per recorded operation, not exactly-once remote execution.

- Same operation ID and hash returns its recorded outcome. Changed request under the same ID conflicts. A new ID cannot bypass an unresolved same-session or logical-task effect.
- Timeout, disconnect, uncertain server error, malformed success and lost acceptance persistence mean unknown. Never automatically retry POST. Clear rejection is recorded without a hidden retry; a later new attempt requires a new decision with predecessor linkage.
- Absence after repeated scans is not proof of no effect. Exact matching manual text is not proof of request attribution. Separate `api_accepted`, `effect_observed`, `attribution` and `ui_verified`.
- A valid create response with unavailable binding verification is `accepted_identity_unverified`. Do not create again. A wrong binding prevents a confirmed-success claim.
- A blocked/rejected/unknown mutation stops following mutations in that invocation. Read reconciliation and independent reads may continue. Failed read dependencies skip only their dependents.
- Partial history cannot advance a complete checkpoint, establish absence or authorize a write. Before/after reads detect some drift but do not create an atomic snapshot.
- Invocation timeout yields `waiting` and a resume reference. It does not end a user's authorized watch. Continue the same job until its requested outcome, cancellation or a real input/permission blocker. Unchanged polls do not trigger model decisions.
- Outbox is at least once. Store receiver acceptance before ACK. If actual channel send succeeds ambiguously without an idempotent lookup, preserve `delivery_unknown` and do not blindly resend.

Exit codes are 0 complete, 2 waiting/yielded, 3 invalid input or fatal read/local failure, 4 blocked/rejected/unknown mutation, 5 partial/unsupported and 130 interrupted. For mixed outcomes choose 130 > 4 > 3 > 5 > 2 > 0, while preserving every action result in JSON. A remote task's `FAILED` state is observed data, not automatically a failed controller call.

## Finite tests and review

Every slice below names a finite required case set. Use synthetic fixtures, deterministic fault injection and fake clocks. Default timeout is 30 seconds per test, 10 minutes per suite and 20 minutes per aggregate CI job. Exceeding a limit is a failed/incomplete check, not permission for an endless rerun. Commands shown below are planned acceptance commands; their modules do not exist in this planning-only repository.

For each implementation slice: run focused checks, have one independent reviewer and one independent challenger inspect the final diff/evidence, allow one correction pass, then rerun affected tests and targeted checks. Pass only if required checks and material findings are resolved; otherwise stop as blocked for a maintainer decision. Never replace failed tests with weakened assertions. S14 repeats aggregate checks on the exact integrated revision. Optional slices require the same standard if enabled.

New tests must be discoverable by aggregate unittest discovery; include package initializers where required. CI cannot access live credentials or network. Development-only schema/type tooling may be version-pinned during S01 without adding a heavy runtime SDK. Every release report records source revision, Python/platform, command, test count, result and unrun checks.

## Executable implementation slices

Each entry is a work packet. It authorizes nothing by itself. An assigned contributor receives the slice, accepted dependency revision, immutable scope and the listed tests.

### S00 Freeze and sanitize baseline
**Wave 0 · Core · offline · 1–2 engineering days**
**Objective:** Obtain the maintainer-provided relay snapshot, reproduce its original 28 offline tests before changes, and prepare a public-safe compatibility baseline.
**Dependencies:** None. **Entry gates:** P0.
**Non-goals:**
- Do not import private documentation, runtime state, live records or raw transcripts.
- Do not refactor behavior or claim the historical test result is current.

**Allowed files:** `skills/relay-jules/scripts/jules_relay.py`, `tests/test_relay.py`, `tests/__init__.py`, `docs/BASELINE.md`, `.gitignore`.

**Interfaces:**
- Legacy CLI flags, JSONL records, exit codes and SQLite tables are the baseline.
- Store original source/test hashes and original test output privately; publish only a sanitized provenance summary and public snapshot hashes.

**Required tests:**
- S00-T01: Run the original suite unchanged against its original snapshot in a private workspace: exactly 28 collected tests must pass; record command, runtime, timestamp and hashes.
- S00-T02: Import only authorized public-safe code and tests; replace identifying fixture labels with OWNER/REPO and feature/example where necessary, with no assertion or behavior changes; rerun the 28-test public mirror.
- S00-T03: Compare command help, exit codes and fixture outputs before/after sanitization, allowing only the documented label substitutions.
- S00-T04: Scan every tracked file for account names, private repositories, private links, session identifiers, secrets, credentials, state databases and machine-specific paths.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests -p test_relay.py -v
```

**Done when:**
- Original baseline is independently rerun; public mirror preserves all 28 test names and assertions.
- Import provenance and any label-only sanitization are explicit. Runtime state is ignored.

**Stop conditions:**
- Missing baseline input or any failing original test blocks the import gate.
- Unexpected behavior changes require a separately scoped defect item; do not weaken baseline assertions.

### S01 Freeze protocol and interfaces
**Wave 1 · Core · offline · 2–3 engineering days**
**Objective:** Freeze strict runtime plan/result schemas, typed internal ports and canonical hashing rules so parallel slices can be implemented without guessing interfaces.
**Dependencies:** S00. **Entry gates:** G0.
**Non-goals:**
- No remote requests, general expression engine, arbitrary URLs or dynamic mutation targets.
- No claim that an example or authorization reference grants permission.

**Allowed files:** `schemas/**`, `examples/**`, `src/octodot/contracts.py`, `src/octodot/models.py`, `src/octodot/errors.py`, `docs/CONTRACTS.md`, `tests/contracts/**`, `requirements-dev.txt`.

**Interfaces:**
- Runtime schemas retain jules-controller.plan.v1 and jules-controller.result.v1; implementation-plan JSON has a separate namespace.
- Ports: Transport, JulesReadAPI, Store, ReadService, GrantVerifier, MutationJournal, ActionHandler, Receiver and optional Provider.
- Records: Binding, Observation, Coverage, CandidateBundle, PreparedAction, VerifiedGrant, OperationRecord, ActionResult, Event, Receipt, ArtifactManifest and Capability.
- Prepare canonical UTF-8 JSON with sorted keys, compact separators, exact Unicode strings and no NaN/Infinity; version the encoding and projections. Binding/context hashes exclude volatile observation timestamps.

**Required tests:**
- S01-T01: Schema examples include a read-only plan, a partial result and disabled reply/create/approval templates; all validate structurally.
- S01-T02: Reject duplicate keys, unknown input fields, invalid types, nonfinite numbers, duplicate action IDs, forward/invalid references, arbitrary JSONPath, dynamic mutation payloads and oversized input.
- S01-T03: Disabled and placeholder-bearing mutation templates fail execution eligibility before credential access.
- S01-T04: Golden canonical-hash vectors cover Unicode, newline differences, object key order, branch case and volatile observation metadata.
- S01-T05: Every read operation and mutation has a typed argument/result contract; unknown remote response fields/states remain representable.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/contracts -p "test_*.py" -v
```

**Done when:**
- Every operation named in the plan has a frozen contract, error code and capability classification.
- Compatibility differences require explicit versioned documentation. Schema validation is dev-only; runtime remains dependency-light.

**Stop conditions:**
- An unresolved interface or authorization-boundary disagreement stops dependent slices.
- Exact-commit pinning or atomic exact-plan approval requirements must be reported as unsupported.

### S02 Extract fixed-origin typed API
**Wave 2 · Core · offline · 2–3 engineering days**
**Objective:** Extract bounded standard-library HTTP and typed Jules wrappers while retaining fixed-origin, TLS, redirect and credential isolation controls.
**Dependencies:** S01. **Entry gates:** G1.
**Non-goals:**
- No raw HTTP CLI, arbitrary endpoint option or automatic POST retry.
- Do not enable live mutations in this slice.

**Allowed files:** `src/octodot/transport.py`, `src/octodot/api.py`, `tests/transport/**`, `tests/api/**`.

**Interfaces:**
- Read methods: sources.list/get, sessions.list/get and activities.list/get.
- Internal mutation methods: create, send_message and approve_plan require a journal-issued dispatch ticket; no public bypass.
- Transport returns typed sanitized outcomes with request/byte counts and uncertain_effect; fixture transport never accesses credentials or network.

**Required tests:**
- S02-T01: Enumerate allowlisted method/path pairs; reject traversal, encoded traversal, wrong host/port/scheme, userinfo, redirects and unknown queries.
- S02-T02: Paginate 0, 1, 100 and 101 records; follow empty pages with continuation; reject token cycles, malformed pages and conflicting duplicate identities.
- S02-T03: Exercise empty success for message/approval, valid session for create and malformed/oversized/truncated response handling.
- S02-T04: Exercise TLS/proxy denial, 401/403, 429 with Retry-After, 5xx, timeouts, disconnects and request/total-byte/deadline caps; do not expose raw bodies/headers.
- S02-T05: Credential spy proves no access in fixtures/validation; synthetic secret absent from stdout, stderr, exceptions, DB and artifacts.
- S02-T06: Every mutation failure path records at most one transport attempt; GET backoff is bounded and respects Retry-After.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/transport -p "test_*.py" -v
python3 -m unittest discover -s tests/api -p "test_*.py" -v
```

**Done when:**
- Typed API passes the endpoint matrix and byte/deadline tests.
- Only safe GET retries exist; cooperative blocked-I/O/DNS deadline limitation is documented.

**Stop conditions:**
- New undocumented endpoint or credential requirement blocks expansion.
- Transport cannot classify possible dispatch as a safe retry.

### S03 Migrate durable SQLite state
**Wave 2 · Core · offline · 3–4 engineering days**
**Objective:** Add versioned durable tables and recovery metadata without losing legacy bindings, activity identities, receipts or unresolved sends.
**Dependencies:** S01. **Entry gates:** G1.
**Non-goals:**
- No multiwriter leases, DB pruning, network filesystem support or transaction held across HTTP/sleep.

**Allowed files:** `src/octodot/store.py`, `src/octodot/migrations/**`, `tests/store/**`, `tests/fixtures/store/**`.

**Interfaces:**
- Store owns short transactions, owner-only state files and a workflow-wide exclusive lock released between wait iterations.
- Migrate bindings/activities/outbox/sends into versioned profiles, scans, checkpoints, jobs, action_results, operations, operation_evidence, authorization_records, receiver_receipts and manifests.
- Recovery fence compares a coordinator-owned profile generation/epoch and journal checkpoint outside the worker-writable DB; no trusted fence means mutation resumption remains disabled. Any change of selected profile or credential configuration must advance that host-controlled epoch, invalidate prior bindings and grant eligibility, and require fresh identity revalidation plus newly eligible grants before writes. Store only the non-secret configuration epoch, never a key or key fingerprint.

**Required tests:**
- S03-T01: Migrate empty and populated legacy DBs containing acknowledged/unacknowledged events and in_flight/accepted/unknown sends; preserve stable IDs and acknowledgements.
- S03-T02: Crash before and after each migration commit and scan-commit boundary; reopen without partial checkpoint or event loss.
- S03-T03: Fail safely on disk-full, locked/corrupt DB, incompatible newer schema, unsafe state directory and unavailable lock support.
- S03-T04: Two processes contend on the global lock; wait release permits receipt handling and a separately authorized action; no transaction spans network/sleep.
- S03-T05: Missing DB, restored stale backup or changed profile generation blocks mutation resumption; current trusted fence restores read-only recovery only until reconciled. Rotate the synthetic credential configuration without changing the profile name: the host epoch must advance, old bindings/grants must fail, and mutation eligibility must remain blocked until fresh source/session identity is verified and a grant for the new epoch is checked; neither key nor key fingerprint may be persisted.
- S03-T06: An incomplete scan can retain partial evidence but cannot advance a complete checkpoint, establish absence or supply write-eligible context.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/store -p "test_*.py" -v
```

**Done when:**
- Migration tests preserve all legacy deduplication and unknown-operation protection.
- Backup/restore procedure and private-state exclusions are documented; loss of durable identity fails closed for mutations.

**Stop conditions:**
- Cannot establish trusted recovery fence: disable writes, continue safe reads.
- Migration data loss, event-ID changes or unknown-to-retry conversion blocks release.

### S04 Implement identity and projections
**Wave 2 · Core · offline · 2–3 engineering days**
**Objective:** Build pure, deterministic repository/branch binding, lifecycle, conversation, plan and failure projections.
**Dependencies:** S01. **Entry gates:** G1.
**Non-goals:**
- No LLM interpretation of remote instructions, automatic semantic answer resolution or question-mark heuristics.

**Allowed files:** `src/octodot/identity.py`, `src/octodot/projections.py`, `tests/identity/**`, `tests/projections/**`.

**Interfaces:**
- Resolve source names from returned structured owner/repo fields; do not construct them from repository text.
- Binding stores profile, canonical source/session, repository and exact starting branch; branch comparison is case-sensitive.
- Projection separates lifecycle, attention, local disposition, delivery and publication; CandidateBundle preserves all relevant messages and ambiguity.

**Required tests:**
- S04-T01: Wrong owner/source, malformed owner/repo types, slash source names, ambiguous matches, repoless sessions and exact branch-case/slash differences.
- S04-T02: Explicit main or master is allowed only as deliberately selected scope; no absent/default-branch substitution. Legacy facade retains its documented explicit override.
- S04-T03: Unknown states remain visible and block state-dependent writes. Exclusive lifecycle buckets are open/completed/failed/unknown; terminal is derived as completed + failed, while attention subsets may overlap.
- S04-T04: Multi-message feedback, messages without question marks, tied nanosecond timestamps, manual reply, incomplete history and before/after session drift remain conservative.
- S04-T05: Current failure, historical failure after recovery, nonzero expected test command, transport error and suspected stall remain distinct.
- S04-T06: Plan content hashes retain exact approved content; unknown activity types are represented without inventing semantics.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/identity -p "test_*.py" -v
python3 -m unittest discover -s tests/projections -p "test_*.py" -v
```

**Done when:**
- Projection outputs match finite golden fixtures and expose chronology/coverage uncertainty.
- No incomplete or ambiguous projection supplies write-eligible context.

**Stop conditions:**
- Ambiguous identity or chronology blocks dependent mutations.
- Material exact-commit requirement cannot be satisfied by a branch string.

### S05 Implement preparation and trust
**Wave 2 · Core · offline · 2–3 engineering days**
**Objective:** Provide read-only preparation and a trusted grant-verification port that binds one precise request and fails closed without a real external authority.
**Dependencies:** S01. **Entry gates:** G1.
**Non-goals:**
- No worker-issued grants, approval booleans as authority, writable local signing key or new permission service deployment.

**Allowed files:** `src/octodot/authorization.py`, `src/octodot/preparation.py`, `tests/authorization/**`, `tests/preparation/**`, `docs/AUTHORIZATION.md`.

**Interfaces:**
- GrantVerifier.verify(reference, prepared_action, current_profile_epoch) returns VerifiedGrant or a typed blocker.
- Grant binds action, operation ID, profile and host-controlled credential-configuration epoch, source/repository/branch/session, exact payload hash, context/plan hash, publication scope, authorizing source, expiry/revocation and max_attempts=1. A profile or credential-configuration switch invalidates old eligibility and requires fresh identity revalidation; no credential value or fingerprint is retained.
- Preparation consumes ReadService observations via its frozen port; no credentials in --validate-only. Trusted adapter is supplied by the authorized host, not invented by the runner.

**Required tests:**
- S05-T01: Missing, malformed, expired, revoked, wrong-profile, wrong-credential-configuration-epoch and wrong-target grants fail before POST. A same-name profile with rotated synthetic credentials cannot reuse an old grant after its host-controlled epoch changes.
- S05-T02: Changed Unicode text, branch case, source, session, operation ID, plan/question hash or publication effect invalidates a grant.
- S05-T03: Worker can modify all local plan/assertion files but cannot manufacture a verified grant; no trust adapter means writes disabled.
- S05-T04: Gateway unavailable or stale recovery fence blocks dispatch; replay and single-attempt claim are durable.
- S05-T05: Preparation hash changes only for material context and request changes; incomplete history cannot be prepared for dispatch.
- S05-T06: Fixtures use an in-memory fake verifier explicitly barred from live mode; no fixture credentials or approval export.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/authorization -p "test_*.py" -v
python3 -m unittest discover -s tests/preparation -p "test_*.py" -v
```

**Done when:**
- Preparation and execution produce identical canonical hashes.
- Threat model names the actual trust boundary and states that same-OS writable assertions are audit evidence only.

**Stop conditions:**
- If the selected host cannot enforce an independent permission boundary, automated mutations stay disabled.
- No permission is inferred from code-review authority, read access or repository ownership.

### S06 Build full-scan read service
**Wave 3 · Core · offline · 2–3 engineering days**
**Objective:** Implement bounded source/session inventory, session inspection and chat collection using complete full scans first.
**Dependencies:** S02, S03, S04. **Entry gates:** G1.
**Non-goals:**
- No timestamp optimization yet, remote mutation or model call inside a poll.

**Allowed files:** `src/octodot/reads.py`, `src/octodot/actions/read.py`, `tests/reads/**`.

**Interfaces:**
- ReadService.collect(scope, limits), inspect(binding) and chats(selection) return typed observations and staged writes.
- Read scope may omit branch to include all branches in OWNER/REPO; every mutation still needs an exact branch.
- Selection references reuse sufficiently fresh observations only within the same run; mutation preflight always rescans.

**Required tests:**
- S06-T01: Repository discovery across 101 sessions, UI/API-origin fixtures, unbindable/repoless entries and new sessions appearing between discovery passes.
- S06-T02: Empty continuing pages, expired/cyclic page tokens, duplicates/conflicts and before/after session changes never claim atomic coverage.
- S06-T03: Request/page/session/byte/output caps produce partial coverage, skipped scope and resume reference; no false complete inventory.
- S06-T04: Partial independent session reads return good results plus typed failures without hiding failed/unknown/attention items.
- S06-T05: Full-scan commit persists activities/projection/checkpoint/events atomically; incomplete scans do not advance completeness.
- S06-T06: Every read plan has zero POST calls, including malicious remote text and suggestions capability checks.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/reads -p "test_*.py" -v
```

**Done when:**
- Inventory and attention match fixture truth with explicit completeness, freshness and snapshot_atomic=false.
- Full scan is the default correctness path and is usable without optional providers.

**Stop conditions:**
- Identity drift, authentication/network policy failure or caps produce typed blockers/partial output, not guessed values.

### S07 Add events and resumable waits
**Wave 4 · Core · offline · 2–3 engineering days**
**Objective:** Persist resumable bounded observation jobs and at-least-once events with explicit receiver and channel receipt stages.
**Dependencies:** S06. **Entry gates:** G1.
**Non-goals:**
- No daemon, scheduler, autonomous user messaging policy or exactly-once delivery claim.

**Allowed files:** `src/octodot/events.py`, `src/octodot/jobs.py`, `src/octodot/receiver.py`, `tests/events/**`, `tests/jobs/**`, `tests/receiver/**`.

**Interfaces:**
- wait predicates: attention, all_terminal, new_events, operation_observed; fixed selection and per-invocation budgets.
- Event IDs derive from resource/event identity or durable transition identity and survive projection migrations.
- Receiver accepts durably before ACK; receiver_accepted, channel_send_accepted and delivery_unknown are separate.

**Required tests:**
- S07-T01: Fake-clock unchanged polling emits no new events; deadlines yield waiting plus the same durable job ID, then resume to the requested condition.
- S07-T02: Discovery cadence finds new sessions when allowed; fixed selection does not silently expand.
- S07-T03: Final terminal scan picks up late artifacts; all_terminal is not publication verified; a publication watch retains its requested predicate.
- S07-T04: Crash after receiver acceptance/before ACK produces deduplicated redelivery; crash around channel send preserves ambiguous delivery without blind resend.
- S07-T05: Lock release between iterations enables ACK and authorized reply; reacquisition reloads current durable state.
- S07-T06: Read backoff handles 429/Retry-After, transient GET failures and cancellation with finite fake-time traces.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/events -p "test_*.py" -v
python3 -m unittest discover -s tests/jobs -p "test_*.py" -v
python3 -m unittest discover -s tests/receiver -p "test_*.py" -v
```

**Done when:**
- Wait return distinguishes invocation budget from user-task completion.
- Receiver acceptance and actual visible delivery are separately testable; no routine poll needs a model decision.

**Stop conditions:**
- Untrusted receiver receipt, lost state or ambiguous send blocks affected delivery claims.
- External watch continues across yielded invocations until predicate, cancellation or a genuine permission/input blocker.

### S08 Implement single-attempt journal
**Wave 3 · Core · offline · 3–4 engineering days**
**Objective:** Centralize durable mutation intent, one local dispatch attempt and read-only uncertain-effect reconciliation.
**Dependencies:** S02, S03, S05. **Entry gates:** G1.
**Non-goals:**
- No retry override, lease-expiry reset, same-session bypass via new ID or simulated rollback of created tasks.

**Allowed files:** `src/octodot/journal.py`, `src/octodot/reconciliation.py`, `tests/journal/**`, `tests/reconciliation/**`.

**Interfaces:**
- State machine: prepared -> dispatching -> accepted -> effect_observed; alternatives blocked_before_dispatch, rejected, unknown, cancelled_before_dispatch.
- Journal commits dispatching before issuing its single transport ticket; abandoned dispatching recovers as unknown.
- Unique operation ID plus canonical request hash; unresolved same-session intent and creation logical-task marker block new IDs.

**Required tests:**
- S08-T01: At each boundary before intent commit, after intent, before dispatching commit, after dispatching commit, after send, after response and during acceptance persistence: kill/restart and prove <=1 local POST attempt.
- S08-T02: Same ID/same hash returns recorded state; same ID/different request conflicts; a new ID cannot bypass unresolved session/logical task.
- S08-T03: Timeout, disconnect, uncertain 5xx, malformed success or response-save failure remains unknown; clear rejection is recorded without automatic retry.
- S08-T04: Exact manual matching message, duplicate text and multiple creation-marker matches yield effect evidence with honest attribution uncertainty.
- S08-T05: No matching effect after 0, 1 or 3 complete scans remains unknown; absence never authorizes retry.
- S08-T06: Missing/stale DB and invalid grants cannot produce a dispatch ticket; desired-state resolution retires a blocker without authorizing resend.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/journal -p "test_*.py" -v
python3 -m unittest discover -s tests/reconciliation -p "test_*.py" -v
```

**Done when:**
- Finite crash matrix proves at most one local attempt per recorded operation across restart.
- Evidence separates api_accepted, effect_observed, attribution and ui_verified.

**Stop conditions:**
- Any path to duplicate local dispatch or false attribution blocks the entire mutation gate.
- Unknown effects permit only reads/reconciliation until trusted resolution.

### S09 Wire ordered runner and CLI
**Wave 5 · Core · offline · 2–3 engineering days**
**Objective:** Implement the versioned ordered action runner and JSON CLI with whole-plan validation and bounded output.
**Dependencies:** S07, S08. **Entry gates:** G1.
**Non-goals:**
- No general runtime DAG, shell actions, arbitrary templating or automatic mutation fan-out.

**Allowed files:** `src/octodot/runner.py`, `src/octodot/cli.py`, `src/octodot/__init__.py`, `src/octodot/__main__.py`, `tests/runner/**`, `tests/cli/**`.

**Interfaces:**
- jules-controller run --plan plan.json --result result.json; prepare --validate-only and prepare --online-preflight.
- Action handlers share typed contracts and a static allowlist; only earlier typed read selections can be referenced.
- Plan IDs bind immutable plan hashes; fresh scans use new runs or explicit resumed jobs.

**Required tests:**
- S09-T01: Invalid last action rejects the whole plan before credentials/network; duplicate/forward refs, unknown fields and dynamic mutation targets fail.
- S09-T02: Failed read dependency skips dependent action; independent reads continue; blocked/rejected/unknown mutation suppresses subsequent mutations while reconciliation reads continue.
- S09-T03: Replay same plan ID/hash returns recorded action/operation status; changed hash conflicts; a new run cannot repeat a recorded mutation.
- S09-T04: Large text/artifacts spill to checksummed bounded private artifacts; capped output names every omitted attention item or marks incomplete coverage.
- S09-T05: Mixed ok/waiting/read-error/mutation-error/unsupported/interrupted cases match deterministic result and exit-code precedence.
- S09-T06: Read-only mode cannot enter a mutation dispatch path or issue POST; disabled templates cannot load any credentials, regardless of action text. Authorized GET mode may load the selected Jules credential.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/runner -p "test_*.py" -v
python3 -m unittest discover -s tests/cli -p "test_*.py" -v
```

**Done when:**
- CLI output is machine-readable JSON/JSONL, sanitized diagnostics use stderr, and every cap is enforced.
- Shorthands compile to the same runner instead of separate unsafe code paths.

**Stop conditions:**
- Any mismatch between structured result and exit status, silent truncation or credential access during offline validation blocks integration.

### S10 Implement exact approved replies
**Wave 4 · Core · offline · 1–2 engineering days**
**Objective:** Implement one exact approved reply with fresh feedback-bundle checks and bounded read-only effect verification.
**Dependencies:** S06, S08. **Entry gates:** G1.
**Non-goals:**
- No message rewriting, guessed recipient/question, semantic answer assumption or live POST in this slice.

**Allowed files:** `src/octodot/actions/reply.py`, `tests/reply/**`.

**Interfaces:**
- ActionHandler chats.reply consumes literal target/payload/preconditions and a trusted grant.
- Preflight: source/session refresh -> full activities -> context hash -> final session check -> journal dispatch -> exact prompt POST once -> reconcile new activity IDs.

**Required tests:**
- S10-T01: Exact approved text and source/repository/branch/session/feedback bundle are required; stale state, changed bundle, partial history and branch drift yield zero POST.
- S10-T02: Multi-message bundle preserved; tied chronology and missing/blank/malformed message content block dispatch.
- S10-T03: Empty successful response is acceptance only; new exact user activity is effect evidence, not proof of attribution/task completion.
- S10-T04: Manual identical message, changed text after authorization, unknown existing intent and restart cannot cause a second attempt.
- S10-T05: Secret-pattern input, unauthorized consequential content and a grant covering a different publication effect fail closed.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/reply -p "test_*.py" -v
```

**Done when:**
- Offline fixtures cover the full reply sequence and preserve original text bytes.
- Normal live reply capability remains disabled until G4 passes. After G3, only the exact P_REPLY-authorized G4 acceptance invocation may run once through the same trusted verifier, recovery fence and journal with max_posts=1; this is not general capability enablement.

**Stop conditions:**
- No real grant or changed context means stop before dispatch.
- Uncertain result means reconcile, never retry.

### S11 Implement bounded task creation
**Wave 4 · Core · offline · 1–2 engineering days**
**Objective:** Implement one literal task creation per action, verified source/starting branch and independent creation evidence.
**Dependencies:** S06, S08. **Entry gates:** G1.
**Non-goals:**
- No bulk nested transaction, repoless creation, default-branch fallback, automatic publication or task rollback.

**Allowed files:** `src/octodot/actions/create.py`, `tests/create/**`.

**Interfaces:**
- tasks.create body contains only approved title/prompt, sourceContext and requirePlanApproval=true. publication=none omits automationMode.
- Preflight enumerates complete existing-session identity set and verifies exact source/branch. Marker must already be in approved text.
- A valid returned session is refreshed to verify binding; failed verification is accepted_identity_unverified, not permission to recreate.

**Required tests:**
- S11-T01: Missing branch, case mismatch, stale/absent branch metadata, unknown source and exact-commit requirement block creation; no branch fallback.
- S11-T02: One action yields exactly one body with supported fields; controller hashes/grants/operation IDs are never invented API fields.
- S11-T03: Unauthorized AUTO_CREATE_PR, false plan-approval flag and prompt-level unapproved publication request fail validation/grant checks.
- S11-T04: Invalid 2xx becomes unknown; valid response plus unavailable GET remains accepted_identity_unverified; wrong binding blocks confirmation.
- S11-T05: Logical-task marker collision, lost response, duplicate run and no candidates after repeated full scans never issue a second POST.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/create -p "test_*.py" -v
```

**Done when:**
- Create serialization and identity verification pass the finite fixtures.
- One action failure leaves earlier successes intact and blocks following mutations.

**Stop conditions:**
- Unverified branch, ambiguous correlation or undocumented server field stops this action.
- Create live gate requires its own task/source/branch/content grant.

### S12 Implement guarded plan approval
**Wave 4 · Core · offline · 1–2 engineering days**
**Objective:** Implement exact reviewed plan preflight and session-level approval with explicit remote race limitations.
**Dependencies:** S06, S08. **Entry gates:** G1.
**Non-goals:**
- No implicit plan approval, scope expansion, publication authorization or atomic exact-plan guarantee.

**Allowed files:** `src/octodot/actions/approve.py`, `tests/approve/**`.

**Interfaces:**
- plans.approve requires latest plan ID/content hash, current waiting state and grant covering the task scope.
- The endpoint receives the session only with an empty request; plan identity is checked locally, then approval activity is read.

**Required tests:**
- S12-T01: Changed/latest plan mismatch, expired grant, wrong state, unknown state and partial history block POST.
- S12-T02: Valid fixture posts once with no invented planId/body; planApproved activity is matched by plan ID.
- S12-T03: Plan changes after final read, wrong planApproved ID or missing event remains inconclusive; do not claim atomic approval.
- S12-T04: Timeout/malformed success/restart and new operation ID cannot bypass an unresolved approval.
- S12-T05: Task scope or publication change requires a new actual authorization, not a transformed grant.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/approve -p "test_*.py" -v
```

**Done when:**
- Offline approval flow reports acceptance and effect separately and keeps live gate disabled.

**Stop conditions:**
- Atomic exact-plan approval requirement is unsupported by this endpoint.
- Unresolved approval or scope drift blocks execution.

### S13 Gate incremental activity reads
**Wave 5 · Optional · offline · 2–3 engineering days**
**Objective:** Add opt-in per-profile createTime optimization with complete pagination, overlap and scheduled full reconciliation.
**Dependencies:** S07. **Entry gates:** G1.
**Non-goals:**
- No correctness dependency on a timestamp cursor, ordering assumption or live capability claim from docs alone.

**Allowed files:** `src/octodot/capabilities.py`, `src/octodot/incremental.py`, `tests/incremental/**`, `docs/CAPABILITIES.md`.

**Interfaces:**
- Capability fields documented/enabled/live_tested plus source and evidence time are independent.
- Keep nanosecond RFC3339 precision, ID deduplication and several-minute configurable overlap; full preflight always uses full history.
- Bootstrap full baseline, compare authorized filtered/full observations, disable optimization on inconsistency; record last complete full reconciliation separately.

**Required tests:**
- S13-T01: Boundary timestamps, tied/nanosecond values, reverse order, empty pages and late activity older than overlap.
- S13-T02: Expired page token or rejected filter falls back to full scan without completeness advance.
- S13-T03: Ignored filter does not lose data; correctness-affecting inconsistency disables it for that profile.
- S13-T04: Crash mid-filtered scan cannot advance checkpoint; recovered projection equals a full-scan oracle over fixed traces.
- S13-T05: Periodic full scan recovers arbitrarily older delayed fixture events; terminal output scan remains mandatory.
- S13-T06: Capability absent/untested starts disabled; global and per-profile disable controls preserve full-scan correctness.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/incremental -p "test_*.py" -v
```

**Done when:**
- Fixture incremental/full projections match; optimization is optional and defaults off until G3-F.

**Stop conditions:**
- Failed live comparison leaves optimization disabled; core read-only release may still pass.

### S14 Integrate and verify offline release
**Wave 6 · Core · offline · 2–3 engineering days**
**Objective:** Assemble the core package, compatibility facade, offline CI and finite reviewer/challenger release evidence.
**Dependencies:** S09, S10, S11, S12. **Entry gates:** G1.
**Non-goals:**
- No deployment, skill installation, configured cloud model, public runtime artifacts or enabled live mutations.

**Allowed files:** `src/octodot/registry.py`, `src/octodot/compat.py`, `pyproject.toml`, `.github/workflows/offline.yml`, `tests/integration/**`, `tests/compat/**`, `tests/publication_safety/**`, `docs/OPERATIONS.md`, `docs/RELEASE_CHECKLIST.md`, `skills/relay-jules/SKILL.md`, `skills/relay-jules/agents/openai.yaml`, `skills/relay-jules/references/**`, `README.md`, `skills/relay-jules/scripts/jules_relay.py`.

**Interfaces:**
- Register fixed handlers from completed slices; optional handlers register only when their own tests pass and capability remains explicit.
- Use the original private snapshot as the frozen compatibility oracle. Convert the public legacy executable into a compatibility facade over the same runner/journal; retain tested import/helper APIs where needed. No public legacy live-send bypass may remain.
- Read commands preserve legacy semantics; legacy --approved assertions cannot open live writes without the new trusted grant boundary. Explicitly document this intentional security tightening.

**Required tests:**
- S14-T01: Run original/public 28-test baseline and complete new offline suites on supported Python 3.10+ Linux/macOS environments; record exact matrix and any unrun platform.
- S14-T02: Golden legacy fixtures and JSONL/exit behavior; intentional authorization tightening has a versioned compatibility notice and explicit blocked result.
- S14-T03: End-to-end read plan, prepare, disabled template, fake-grant reply/create/approve, crash/restart, unknown reconciliation and resumed wait.
- S14-T04: Negative matrix proves zero live network and credential access in CI, no POST in read-only mode and <=1 fixture POST for every mutation fault trace.
- S14-T05: Packaging includes only source, tests, schemas, public docs and synthetic examples; reject SQLite, locks, fixture call logs, private transcripts, real IDs or secrets.
- S14-T06: One independent reviewer and one independent challenger inspect the final diff and fault evidence; one correction round and targeted recheck, then pass or explicit blocked report.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/integration -p "test_*.py" -v
python3 -m unittest discover -s tests/compat -p "test_*.py" -v
python3 -m unittest discover -s tests/publication_safety -p "test_*.py" -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q src skills/relay-jules/scripts
python3 -m octodot --help
```

**Done when:**
- G2 evidence names final source revision, commands, counts and unrun checks. No unresolved unsafe-dispatch, data-loss or false-success finding remains.
- Portable package works without daemon/application-repo installation; skill metadata makes no model-routing claim.

**Stop conditions:**
- More correction cycles or new scope requires a maintainer decision; do not run an open-ended review loop.
- Missing trusted authorization adapter blocks future writes but need not block a clearly read-only release.

### S15 Prove live read-only parity
**Wave 7 · Core · live read only · 0.5–1 engineering days**
**Objective:** With already-authorized read access, verify one existing UI-created session and activity history via GET and record sanitized gate evidence.
**Dependencies:** S14. **Entry gates:** G2, P_READ.
**Non-goals:**
- No POST, session creation, approval, reply, proactivity toggle or manufactured task failure.
- No private UI screenshot, transcript, session ID or account identity in the public repository.

**Allowed files:** `docs/evidence/G3-read-only.summary.json`, `docs/evidence/G3-filter.summary.json`.

**Interfaces:**
- Private evidence binds exact profile/source/repo/branch/session; public summary includes gate status, software revision, timestamp, counts and limitations only.
- Match source/session and selected UI message/plan to API activities; record same-session binding and coverage.
- If S13 is present, perform optional G3-F full/filtered comparison; absence or failure leaves optimization disabled.

**Required tests:**
- S15-T01: Within a 180-second, 120-request, 100-page, 200-session, 32-MiB total, 8-MiB response budget: GET health, full discovery and exact existing session/activities; zero POST.
- S15-T02: Match one actual UI message and exact starting branch with API evidence; distinguish UI visibility from actual event delivery.
- S15-T03: If UI access is unavailable, mark parity gate blocked rather than claiming full success.
- S15-T04: Optional filter comparison uses a bounded static window plus before/after full scans; any insufficient evidence stays untested and does not enable filtering.

**Done when:**
- G3 passes only when identity, read coverage and same-session UI/API correspondence are evidenced.
- A GET-only release may be labelled live read verified; no two-way relay claim.

**Stop conditions:**
- Auth/proxy denial: respect controls and report exact class; no alternate-host/security bypass.
- Caps or moving history require a resumed read job or a smaller explicitly selected scope, never false completeness.

### S16 Prove authorized reply round trip
**Wave 8 · Core · live mutation · 0.5–1 engineering days**
**Objective:** Prove one harmless exact approved reply through an actual authorized receiver/channel and the same UI session.
**Dependencies:** S15. **Entry gates:** G3, P_REPLY.
**Non-goals:**
- No create/approval/publication, damaging task to provoke a failure, or another POST after uncertainty.

**Allowed files:** `docs/evidence/G4-reply.summary.json`.

**Interfaces:**
- Private gate packet binds exact session/source/repo/branch, current UI/API question bundle, text/hash, one-attempt grant and permission for receiver/channel delivery.
- Trace UI question -> API activity -> durable event -> real receiver acceptance -> authorized channel receipt -> one reply POST -> matching new activity -> same UI conversation.

**Required tests:**
- S16-T01: Precheck G2/G3, trusted GrantVerifier, recovery fence and unresolved-intent absence; absent authorization returns blocked before credential-backed dispatch.
- S16-T02: Allow at most one reply POST with a 180-second/120-request verification invocation; exact approved text and current context only.
- S16-T03: Verify actual channel receipt and same UI text with private evidence; stdout and receiver ACK alone cannot pass.
- S16-T04: If API accepted/effect/UI attribution differs, record each field separately; unknown remains unresolved and uses read-only reconciliation.
- S16-T05: Rerun offline reply fault tests at the exact gate revision; do not reproduce damaging failures live.

**Done when:**
- G4 passes only when every round-trip leg is verified without ambiguity.
- Only after G4 may that tested configuration claim a working two-way reply relay.

**Stop conditions:**
- Missing permission, stale context, actual transport gap or uncertainty blocks rollout immediately.
- Invocation deadline yields a reconciliation job; no expiry converts unknown into safe retry.

### S17 Prove optional task creation
**Wave 9 · Optional · live mutation · 0.5–1 engineering days**
**Objective:** Create one specifically authorized bounded test task on an exact branch with plan approval required and no automatic publication.
**Dependencies:** S16. **Entry gates:** G4, P_CREATE.
**Non-goals:**
- No plan approval, second task, destructive test, commit-pinning claim or publication.

**Allowed files:** `docs/evidence/G5-create.summary.json`.

**Interfaces:**
- Separate create grant names approved task/title/prompt/marker, profile/source/repository/exact starting branch and publication:none. After G4, the exact P_CREATE-authorized G5 acceptance invocation may dispatch once through the normal verifier/fence/journal with max_posts=1 while general creation capability remains disabled; passing G5 enables only separately granted normal creation.
- Verify returned session binding, then observe the generated plan without approving it.

**Required tests:**
- S17-T01: Fresh source and affirmative branch evidence, complete reconciliation preflight and no unresolved logical-task marker.
- S17-T02: At most one create POST; body requires requirePlanApproval=true and omits automationMode.
- S17-T03: GET returned identity/source/branch and observe plan; accepted_identity_unverified or unknown is not success.
- S17-T04: Repeat command offline and restart fault fixtures prove no duplicate work; no duplicate live experiment.

**Done when:**
- G5 records verified creation and waiting plan independently; optional approval remains disabled.
- Core read/reply acceptance does not depend on this gate.

**Stop conditions:**
- No create-specific grant or unverified branch means zero POST.
- Unknown creation blocks another task even when repeated scans find nothing.

### S18 Prove optional plan approval
**Wave 10 · Optional · live mutation · 0.5–1 engineering days**
**Objective:** With a separate decision, approve the exact reviewed plan within the authorized test task scope and observe its approval event.
**Dependencies:** S17. **Entry gates:** G5, P_APPROVE.
**Non-goals:**
- No inferred authorization from creation, exact-plan atomicity claim, publication or merge/deploy.

**Allowed files:** `docs/evidence/G6-approve.summary.json`.

**Interfaces:**
- Separate grant binds session, current plan ID/hash and consequential task scope; record known session-only endpoint race. After G5, the exact P_APPROVE-authorized G6 acceptance invocation may dispatch once through the normal verifier/fence/journal with max_posts=1 while general approval capability remains disabled; passing G6 never removes per-action grant checks.
- Private evidence correlates the observed planApproved event with reviewed plan ID, without claiming exclusive client attribution.

**Required tests:**
- S18-T01: Re-read complete current plan and state immediately before dispatch; stale or ambiguous context blocks.
- S18-T02: At most one approval POST and bounded read verification; match the intended plan ID in observed approval evidence.
- S18-T03: Observe execution state separately from API acceptance; no observed event means inconclusive, not success.
- S18-T04: No publication behavior is inferred from approval; if the user requires atomic exact-plan approval, return unsupported before POST.

**Done when:**
- G6 records approval evidence for that configuration only; later execution/publication outcomes remain separate.

**Stop conditions:**
- Missing approval-specific permission or plan drift blocks dispatch.
- Unknown effect stays read-only reconciliation; no repeat approval.

### S19 Add optional suggestion providers
**Wave 4 · Optional · offline · 2–3 engineering days**
**Objective:** Return explicit unsupported API suggestions and offer opt-in read-only UI/import provider contracts with provenance.
**Dependencies:** S06. **Entry gates:** G1.
**Non-goals:**
- No private API reverse engineering, enabling Proactivity, Start/dismiss actions or creating an analysis session as a substitute.

**Allowed files:** `src/octodot/providers/suggestions.py`, `tests/suggestions/**`, `docs/SUGGESTIONS.md`.

**Interfaces:**
- API provider: status=unsupported, code=unsupported_public_api, coverage.complete=false.
- UI/import: repository, observed_at, provider, source provenance, completeness, cards and stable evidence identifiers; runtime/browser adapter supplied separately.

**Required tests:**
- S19-T01: API mode returns unsupported instead of an empty success while task inventory remains independently complete.
- S19-T02: UI fixtures: logged-out, wrong repository, hidden/virtualized cards, partial scrolling and stale view produce incomplete/unavailable results.
- S19-T03: Import fixtures reject wrong repository, unknown format and stale observation as current truth.
- S19-T04: Interaction audit proves no Start, dismiss, toggle, submit or write; injected page/card instructions cannot alter actions.
- S19-T05: All provider evidence is size-bounded and private; source links/URLs do not bypass fetch policy.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/suggestions -p "test_*.py" -v
```

**Done when:**
- Optional provider can fail without impairing core reads; observed suggestions have honest coverage and provenance.

**Stop conditions:**
- No authorized UI access or reliable repository identity means unavailable; no silent fallback.

### S20 Add inert patch export
**Wave 4 · Optional · offline · 1–2 engineering days**
**Objective:** Export one exact selected patch with reproducible bytes and source/base-commit provenance.
**Dependencies:** S06. **Entry gates:** G1.
**Non-goals:**
- No apply, execute, concatenate historical patches, commit, push or assumed applicability.

**Allowed files:** `src/octodot/providers/patches.py`, `tests/patches/**`, `docs/PATCH_EXPORT.md`.

**Interfaces:**
- Select session/activity/artifact index and verify changeSet.source binding; preserve unidiffPatch UTF-8 bytes and line endings.
- Manifest: activity/index, source, baseCommitId, byte_count, SHA-256 and observation time; generated filename only, private safe directory, atomic write.

**Required tests:**
- S20-T01: Reject wrong source/repository, ambiguous artifact selection and malformed patch type.
- S20-T02: Ignore supplied filenames and embedded URLs; reject traversal, symlinks and unsafe artifact directory.
- S20-T03: Preserve CRLF/LF and non-ASCII bytes exactly; finite large/binary patch fixtures enforce caps and clear unsupported results.
- S20-T04: Duplicate/conflicting artifacts cannot be silently merged; manifest hash matches exported bytes.
- S20-T05: Applicability remains unverified until a separate authorized workspace checks the exact base; passing apply-check would not prove code/tests.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/patches -p "test_*.py" -v
```

**Done when:**
- Patch bytes and manifest verify reproducibly; artifacts remain private/inert.

**Stop conditions:**
- Ambiguous selection, unsafe path or unknown exact base prevents stronger applicability claims.

### S21 Add read-only PR verification
**Wave 4 · Optional · offline · 1–2 engineering days**
**Objective:** Independently verify a Jules-reported PR against GitHub identity, refs, head SHA and checks.
**Dependencies:** S06. **Entry gates:** G1.
**Non-goals:**
- No merge, deploy, review posting, PR creation, automatic publication or inferred merge readiness.

**Allowed files:** `src/octodot/providers/github.py`, `tests/github/**`, `docs/PUBLICATION_VERIFICATION.md`.

**Interfaces:**
- Separate GitHub read adapter uses authorized GitHub access and never receives the Jules key.
- Validate reported URL host/repository/PR, read current base/head/draft/open/merged, page check runs and statuses for exact SHA, then reread head.
- Fields: jules_completed, patch_available, pr_reported, pr_verified, expected_base_match, expected_head_match, checks_sha, checks_status, coverage and drift.

**Required tests:**
- S21-T01: Wrong host/repository/PR, inaccessible private PR and missing reported URL stay unverified/not observed.
- S21-T02: Compare explicit expected base/head only; never infer from starting branch.
- S21-T03: Head changes during check retrieval cause drift and invalidate a current-check claim.
- S21-T04: Paginate checks/statuses, pending/failure/skipped/neutral/cancelled cases and insufficient required-check knowledge; success alone does not imply merge readiness.
- S21-T05: Completed session without PR is not proof of no push; reported PR without GitHub read is not verified publication.

**Planned verification commands:**
```sh
python3 -m unittest discover -s tests/github -p "test_*.py" -v
```

**Done when:**
- All publication claims are scoped to current independently read GitHub evidence.
- Core release remains usable when GitHub access is unavailable.

**Stop conditions:**
- Unknown permissions, missing check coverage or head drift prevents readiness claim; keep read-only.

## Release levels and measurements

- Planning: these documents only; no runtime or acceptance claim.
- Offline candidate after G2: portable tested core, all live mutations disabled.
- Read-verified after G3: the tested profile/environment supports the evidenced GET workflow; no two-way claim.
- Reply-verified after G4: the tested configuration supports one proven two-way reply path. Future replies still need current grants and preconditions.
- Optional creation/approval after G5/G6: enable only individually proven capabilities under their distinct permission scopes. Neither is required for the basic read/reply release.

Record API requests/bytes, scan completeness, full versus incremental work, unchanged polls, event latency, unknown operations and time to reconciliation. Measure coordinator/worker invocations rather than assuming model routing. A normal batch should require one coordinator preparation and one unchanged worker execution; routine HTTP and polling use no model. Optimize only after correctness gates.

## Public repository hygiene

Only publish source, synthetic tests, schemas and public-safe documentation after review. Use `OWNER/REPO`, `feature/example`, `sessions/EXAMPLE` and unissued authorization placeholders in examples. Never commit actual account/email, private repository or branch identifiers, real session/activity IDs, provider secrets, environment IDs, machine-specific paths, private links, raw transcripts, SQLite state, receipts containing private content or live screenshots.

Public gate summaries are facts about verification, not permission artifacts. Publish status, software revision, UTC time, aggregate counts and limitations; keep exact grants, identities and evidence in the authorized private store. Secret scanning is defense in depth and does not replace manual public-disclosure review. No LICENSE or SPDX license choice is implied by repository visibility.

## Official references

The links below support API facts, not claims of live interoperability. Recheck current documentation at the implementation revision. Plan-specific safety and sequencing rules are design decisions.

- [Jules REST inventory](https://developers.google.com/jules/api/reference/rest)
- [Jules sessions](https://jules.google/docs/api/reference/sessions/)
- [Jules sources](https://jules.google/docs/api/reference/sources/)
- [Jules activities](https://jules.google/docs/api/reference/activities/)
- [Jules timestamp filter announcement](https://jules.google/docs/changelog/2026-01-26-4)
- [Jules resource types](https://jules.google/docs/api/reference/types/)
- [sendMessage method](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/sendMessage)
- [approvePlan method](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/approvePlan)
- [Jules Suggested Tasks product guide](https://jules.google/docs/suggested-tasks/)
- [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)
- [GitHub check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
- [GitHub commit statuses](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference)
