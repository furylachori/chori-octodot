# Codex + Octodot: One-Shot Jules Delivery, Monitoring, and Clean Handoffs

This document defines the operational policy, state machine, assignment contracts, review standards, and coordinator reconciliation rules for pairing a Codex coordinator with stateless `octodot.py` to drive Google Jules sessions.

---

## 1. Goal and Architectural Boundaries

The goal is to make stateless `octodot.py` a reliable execution engine for a Codex coordinator:
- **One-shot implementation deliveries**: Jules is invoked to produce a complete substantive delivery, not micro-managed across multiple interactive round-trips.
- **In-flight clarification**: The coordinator answers questions within the approved task scope before delivery.
- **Clean handoffs**: Once a delivered patch requires changes, the coordinator reviews the exact head and launches a **fresh successor session** equipped with confirmed defects, reproduction steps, and accepted base code. The coordinator never steers an old session through PR comments.
- **Zero framework bloat**: No SQLite databases, daemons, background schedulers, or complex orchestration engines inside Octodot. The coordinator maintains its own durable tracking outside Octodot.
- **Honest reporting**: Unexercised live paths are labeled `fixture-tested/unexercised`. Missing credentials, rate limits, or lost acknowledgments are truthfully reported.

---

## 2. Core Operating Decisions & Jules Constraints

1. **One-Shot Delivery**: Jules is one-shot per substantive implementation delivery, not one message. Before delivery, the coordinator may answer clarifying questions strictly within the approved task scope.
2. **Successor Sessions for Corrections**: If a delivered pull request requires code changes, review the exact head SHA and spawn a fresh Jules session from that PR branch with confirmed defects and affected checks.
3. **Mention-Only Behavior on GitHub**: Jules is configured to react only when explicitly mentioned (`@Jules`). When creating review notes or handing off an abandoned session, **do not mention Jules** on the old PR. Plain human-readable review notes remain useful without triggering unwanted Jules re-runs.
4. **No Programmatic Pause**: The Jules public REST API exposes a `PAUSED` state in session objects, but provides no pause or resume endpoint. The coordinator continues without programmatic pause; session deletion is **never** substituted for pausing.
5. **CI Auto-Fixing Awareness**: Mention-only reactivity does not prove CI auto-fixing is disabled on the repository. If an old session reactivates or pushes new commits to its branch, the coordinator halts conflicting integration and reconciles immediately.
6. **Accepted Fixes Stay Closed**: Once a slice or finding is reviewed and accepted, it remains closed. Optional aesthetic polish does not block merging the working base.

---

## 3. Assignment and Setup Contract

Before creating any Jules session, the coordinator establishes and records:
- **Repository Context**: Explicit repository source (`sources/...`), starting branch, and observed head commit SHA.
- **Task Scope**: Clear goal, explicit exclusions, and completion criteria.
- **Environment & Setup**: Verified repository setup snapshot and required setup commands. Missing dependencies are diagnosed upfront rather than rediscovered during remote execution.
- **Validation Set**: Focused test suite and verification commands to validate changes.
- **Publication Permission**: Session creation (`-new`) automatically publishes pull requests. Sessions are created **only** when pull request publication is authorized.
- **Correlation Marker**: An unguessable correlation identifier embedded in the title/prompt to enable unambiguous attribution during reconciliation.
- **Single-Session Default**: Exactly one session is created per assignment. Parallel alternatives require explicit intent and are never used to retry uncertain creation.

---

## 4. Inventory Monitoring and Unblocking

The coordinator polls session inventory periodically (e.g. every 5 minutes while work is active, with exponential backoff on HTTP 429 quota exhaustion).

### Minimum Retained Session Record
For each managed session, the coordinator tracks:
- `sessionName` (`sessions/{id}`)
- `repository` / `source` / `branch`
- `assignmentOwner`
- `parentSessionId` / `successorSessionId`
- `lastObservedState` and timestamp
- `handledActivityIds` (set of activity IDs already processed)
- `outstandingAttention` (pending question, plan, or review)
- `deliveredPR` (URL, base, head SHA)
- `inFlightMutation` (pending reply, approval, or successor dispatch)
- `assistanceType` (none/unaided, reply, plan_approval, successor)

### Session State Classification Matrix
Using `octodot.classify_coordinator_session(session, activities)`:

| Category | Conditions | Coordinator Action |
|---|---|---|
| `working` | `IN_PROGRESS`, `QUEUED`, `PLANNING` with no pending prompts | Observe progress; do not intervene. |
| `waiting_for_user` | `AWAITING_USER_INPUT` or activity has unanswered `userQuery` | Answer repository facts or settled choices via `octodot -reply`. Escalate scope/spend/design changes to owner. |
| `awaiting_plan_approval` | `AWAITING_PLAN_APPROVAL` or activity has unapproved `plan` | If plan matches approved scope, approve via `octodot -approve-plan`. If plan exceeds scope, ask owner. |
| `paused` | Session state is `PAUSED` | Log status and notify owner; do not attempt programmatic resume or delete. |
| `failed` | State is `FAILED` or `CANCELLED` | Check later activities for recovery. If terminal, document failure receipts and prepare successor if authorized. |
| `delivered_awaiting_review` | `COMPLETED` / `SUCCEEDED` with verified PR URL | Inspect PR diff, head commit SHA, and CI checks. |
| `delivered_no_pr` | `COMPLETED` with patch changeSet but no PR | Export patch via `octodot -pull --json` and inspect locally. |
| `completed_empty` | `COMPLETED` with no patch or PR | Check for partial pagination or work underway. Allow 1 in-session clarification before escalating. |
| `handed_off` | Successor spawned; old session abandoned | Retain in roster. If new activity appears (`reactivated=true`), flag immediately. |

### Answering Questions & Approving Plans
- **Safe Answers**: Repository layout, existing code conventions, file locations, and settled architectural decisions may be answered once.
- **Owner Decisions**: Any change to project scope, new financial spend, destructive file operations, or permission escalations must be directed to the repository owner.
- **Plan Approvals**: Plan approval acknowledges that the plan is in scope. It does not replace code review of the finished patch.

---

## 5. Delivery Review & Successor Workflow

When a session finishes with a pull request:
1. **Full Retrieval**: Call `octodot -results` and `octodot -activities` to retrieve complete outputs and full pagination history.
2. **Snapshot Verification**: Fetch the actual PR branch in Git:
   - PR base branch and target commit.
   - Exact head commit SHA.
   - Patch SHA256 digest (`patchSha256`).
3. **Independent Review**: Review the code against task requirements using the primary reviewer and independent challenger.
4. **Clean Handoff to Successor**:
   - If defects are found, assemble a bounded successor assignment:
     - Confirmed defects and exact reproduction steps.
     - Affected file paths.
     - Accepted base code to preserve (do not reopen accepted fixes).
     - Minimal required test commands.
   - Re-fetch the delivered PR branch head immediately before dispatching the successor.
   - Launch the successor starting from the **delivered PR branch** (not `main`), preserving branch ancestry.
   - Link parent and successor IDs in coordinator records.
   - Post review findings to the parent PR without mentioning Jules (`@Jules`).
5. **Stacked PR Handling**: Successors create stacked PRs against the parent branch. The coordinator inspects cumulative diffs and retargets/merges only with explicit owner authorization.

---

## 6. Documented Coordinator Replay Scenarios

The coordinator handles 9 edge cases verified by replay test fixtures:

1. **Paginated Question Buried Before Newer Progress**:
   - *Scenario*: Jules asks a clarifying question on page 1 of activities, but subsequent activities report background progress or tool invocations.
   - *Handling*: The coordinator always inspects full activity pagination rather than only `latestActivity`. `classify_coordinator_session` identifies the outstanding user query.
2. **Duplicate Activity Deduplication**:
   - *Scenario*: Jules API returns identical activity IDs across page boundaries or recurring poll intervals.
   - *Handling*: The coordinator maintains a durable set of `handledActivityIds`. Activities already in the set are ignored, preventing duplicate replies or notifications.
3. **Lost Acknowledgment / Restart Reconciliation**:
   - *Scenario*: A `send_reply` or `approve_plan` call is dispatched but the connection drops or times out (exit 5 / unconfirmed write).
   - *Handling*: On restart, the coordinator does not blindly resend. It queries `read_session` and `read_activities`. If Jules is already working or has recorded the reply, the write is treated as delivered; otherwise, it requires explicit operator confirmation.
4. **Manual Unadopted Session**:
   - *Scenario*: `list-sessions` discovers an unadopted session started manually by a user or another tool.
   - *Handling*: Handled strictly read-only. The coordinator monitors progress without dispatching automated replies or approvals unless explicitly adopted into the roster.
5. **Provider Error Then Recovery**:
   - *Scenario*: Jules logs an error activity or transient backend failure, but later activities indicate continued planning or code generation.
   - *Handling*: Error text alone does not trigger session abandonment. The coordinator checks subsequent activity timestamps and state before concluding failure.
6. **Empty Delivery vs. Partial Retrieval**:
   - *Scenario*: A session finishes, but pagination returns 0 artifacts on page 1 while `complete=false` or results are still buffering.
   - *Handling*: An empty delivery is diagnosed only when `complete=true`, `outputs` contains no PR, and `changeSets` is definitively empty. Incomplete pagination triggers a backoff retry.
7. **Moved Branch Detected Before Successor**:
   - *Scenario*: A PR branch receives an external commit between review time and successor creation time.
   - *Handling*: Head verification re-checks the remote branch SHA immediately before `octodot -new --branch ...`. If the SHA changed, creation is aborted for reconciliation.
8. **Stacked PR Ancestry**:
   - *Scenario*: A successor session creates a secondary PR on top of the parent PR's branch.
   - *Handling*: The coordinator verifies git ancestry (`baseCommitId` matches parent PR head) to confirm a valid stack, ensuring changes are reviewed cumulatively.
9. **Old-Session Reactivation**:
   - *Scenario*: An abandoned session that was handed off unexpectedly wakes up and emits new activity (e.g. delayed worker execution or CI hook).
   - *Handling*: When `handed_off=true`, any new unseen activity ID raises `reactivated=true`, alerting the coordinator to halt competing integration.

---

## 7. Audit Receipts and Rollout Reporting

All mutations (`reply`, `approve-plan`, `new`) output standardized JSON envelopes:
- Every stdout emission includes `dispatched`, `acknowledged`, `operation`, and `outcome`.
- All credentials are redacted (`[REDACTED]`).
- When running in an environment without live credentials, the coordinator labels the rollout status as `fixture-tested/unexercised`.
