# octodot Operations & Reference Guide

This document describes the operational contract, CLI grammar, output envelopes, exit codes, concurrency controls, reconciliation procedures, and Git mutation safety guarantees for `octodot.py`.

---

## 1. Overview & Architecture

`octodot.py` is a single-file, stateless CLI client for the Google Jules REST API (`https://jules.googleapis.com/v1alpha`).

### Key Operational Characteristics

- **Zero Runtime Dependencies**: Built exclusively on the Python 3.10+ standard library.
- **Stateless Execution**: Maintains no SQLite database, daemon process, background scheduler, or session journal.
- **Git Boundaries**: Git is invoked solely for local repository inference (`-new`), patch application (`-pull --apply`), and fresh clone setup (`-teleport`).
- **Single-Attempt Writes**: Session creation (`POST /sessions`) is strictly single-attempt. It is never automatically retried upon network error, timeout, or server error.
- **Authentication**: Authenticated exclusively via the `JULES_API_KEY` environment variable.

---

## 2. CLI Grammar & Invocations

### Syntax

```bash
python3 octodot.py <action> [options...]
```

Exactly one action must be specified per invocation. Both single-dash and double-dash spellings are supported for all actions and flags (except `--version` and `-h`/`--help`).

### Supported Actions

| Action | Alias | Argument | Description |
|---|---|---|---|
| `-new` | `--new` | None | Create one or more alternative Jules sessions. |
| `-list-repos` | `--list-repos` | None | List connected repository sources from the Jules API. |
| `-list-sessions` | `--list-sessions` | None | List existing sessions for the authenticated account. |
| `-status` | `--status` | `SESSION` | Read raw session details (single `GET /sessions/S`). |
| `-activities` | `--activities` | `SESSION` | Fetch all activities for a session via full pagination. |
| `-results` | `--results` | `SESSION` | Fetch comprehensive results, classification, outputs, and patch candidates. |
| `-pull` | `--pull` | `SESSION` | Export unidiff patch (raw text or JSON envelope) or apply locally. |
| `-teleport` | `--teleport` | `SESSION` | Clone repository to a new directory and apply patch on a new branch. |
| `-reply` | `--reply` | `SESSION` | Send message to Jules session (`POST /sessions/S:sendMessage`). |
| `-approve-plan` | `--approve-plan` | `SESSION` | Approve pending plan for Jules session (`POST /sessions/S:approvePlan`). |
| `-h` | `--help` | None | Display usage information and exit. |
| (None) | `--version` | None | Print version (`octodot 1.0.0`) and exit. |

### Argument Applicability Matrix

| Option | Valid Actions | Default | Description |
|---|---|---|---|
| `-prompt`, `--prompt TEXT` | `new`, `reply` | None / Stdin | Instructions for Jules (or `-` for stdin). |
| `--repo REPO` | `new` only | Inferred | Repository in `OWNER/REPO` or `.` format. |
| `--branch BRANCH` | `new` only | Inferred | Starting branch name. |
| `--parallel N` | `new` only | `1` | Total alternative sessions requested (integer 1–100). |
| `--title TEXT` | `new` only | None | Optional session title. |
| `--json` | `pull` (without `--apply`) | `False` | Emit structured JSON envelope instead of raw patch. |
| `--activity RESOURCE` | `pull`, `teleport` | None | Activity selector (`sessions/S/activities/A`). |
| `--artifact INDEX` | `pull`, `teleport` | None | Artifact array index (integer $\ge 0$). |
| `--apply` | `pull`, `teleport` | `False` | Apply patch locally (mandatory for `teleport`). |
| `--cwd DIR` | `new`, `pull --apply` | Process CWD | Target repository directory. |
| `--dir DIR` | `teleport` only | (Mandatory) | Destination directory for teleport clone. |
| `--timeout SECONDS` | All API actions | `30` | Per-request socket/HTTP and subprocess timeout. |
| `--deadline SECONDS` | All API actions | `120` | Total monotonic admission budget for the invocation. |

### Strict Option Rules

- **Exclusivity**: Specifying multiple actions is an exit 2 error.
- **Duplicate Options**: Any option specified more than once (including across aliases, e.g. `-prompt A --prompt B`) produces an immediate exit 2 error.
- **`--json` Restrictions**: `--json` is valid only for `pull` without `--apply`.
- **Selector Pairing**: `--activity` and `--artifact` must always appear together.
- **`--cwd` Restrictions**: `--cwd` cannot be combined with explicit `OWNER/REPO` on `new`, and cannot be used with `pull` without `--apply`.
- **Finite Numerics**: `--timeout` and `--deadline` must be finite positive numbers. Zero, negative numbers, `NaN`, and `Infinity` are rejected.

### Prompt Handling Precedence

1. **Literal Prompt (`-prompt "..."`)**: Takes strict precedence over stdin. It never reads from stdin, even if data is piped into the process.
2. **Explicit Stdin (`-prompt -`)**: Reads standard input until EOF. Invocations on an interactive TTY are rejected with exit 2.
3. **Piped Non-TTY Stdin**: If `-prompt` is omitted and standard input is redirected or piped, stdin is read until EOF.
4. **Interactive TTY Omission**: Omitting `-prompt` when stdin is an interactive TTY results in exit 2.

---

## 3. Exit Codes & Precedence

When multiple failure conditions arise, exit codes are determined by the following strict precedence:

$$\mathbf{130} \text{ (Interruption)} > \mathbf{5} \text{ (Uncertain / Mismatch)} > \mathbf{3} \text{ (Auth / Configuration)} > \mathbf{4} \text{ (Transport / Protocol / Git)} > \mathbf{2} \text{ (Usage / CLI)} > \mathbf{0} \text{ (Success)}$$

### Exit Code Definitions

| Code | Meaning | Examples / Triggers |
|:---:|:---|:---|
| **0** | **Success** | All requested steps succeeded. (For `results`, remote task failure or absence of PR is still exit 0 if all API reads succeeded). |
| **2** | **CLI / Local Input Error** | Unknown flag, invalid syntax, conflicting options, empty prompt on TTY, invalid repo/branch format, non-finite timeout. |
| **3** | **Auth / Environment Gate** | Missing or invalid `JULES_API_KEY`, HTTP 401 Unauthorized, HTTP 403 Forbidden, Git version < 2.36, unsupported OS for `--apply`. |
| **4** | **Transport / Protocol / Git Error** | HTTP 5xx, transport exception, rate limits (HTTP 429), malformed API JSON, deadline exceeded, Git apply conflict, broken pipe. |
| **5** | **Uncertain / Context Mismatch** | POST unconfirmed (timeout, transport error, or disconnect during POST), returned session name missing/invalid, returned source/branch mismatch, unconfirmed interaction writes. |
| **130** | **Interrupted** | Process received `SIGINT` (Ctrl+C) or `SIGTERM`. Handlers drain active workers or record unconfirmed/never_dispatched write status and exit 130. |

---

## 4. Standardized Output Envelopes

All JSON output is UTF-8 encoded with `ensure_ascii=False`, sorted keys, compact separators (`", "` and `": "`), and terminated with a single newline. No terminal colors are used.

### Shared Thread Lock

All stdout and stderr JSON emissions share a single process-level `threading.Lock` to guarantee whole-line atomicity and prevent interleaved output across concurrent workers.

### Recursive Key Redaction

Every string in stdout and stderr output is recursively inspected. Any occurrence of the active `JULES_API_KEY` is redacted and replaced with `[REDACTED]`.

---

### Non-New JSON Actions

All actions other than `new` and raw `pull` emit a single JSON envelope on stdout:

```json
{
  "action": "status",
  "ok": true,
  "complete": true,
  "data": { ... },
  "error": null
}
```

- `action`: Name of the executed action (e.g. `"status"`, `"results"`, `"pull"`).
- `ok`: `true` if transport and operations succeeded without client/transport error.
- `complete`: `true` if all requested resources and pages were fully retrieved.
- `data`: Action-specific payload object (or `null` on failure).
- `error`: `null` on success, or an error record object.

#### Error Object Structure

```json
{
  "kind": "transport_error",
  "message": "Connection timed out",
  "httpStatus": 408,
  "operation": "get_session",
  "provider": {
    "code": 408,
    "status": "DEADLINE_EXCEEDED",
    "message": "Backend deadline exceeded"
  }
}
```

- `kind`: Internal error classification (e.g. `"auth_error"`, `"not_found"`, `"protocol_error"`).
- `message`: Fixed, human-readable client message.
- `httpStatus`: HTTP status code (integer or `null`).
- `operation`: Name of the API or local operation during which failure occurred.
- `provider`: Sanitized provider error details from Google (or `null` if not an API error). Provider `message` is redacted and truncated to 2048 characters.

---

### Action Data Shapes

#### 1. `list-repos`
```json
{
  "sources": [
    {
      "name": "sources/github/OWNER/REPO",
      "githubRepo": { "owner": "OWNER", "repo": "REPO" },
      "defaultBranch": { "displayName": "main" },
      "branches": [ { "displayName": "main" }, { "displayName": "dev" } ]
    }
  ]
}
```

#### 2. `list-sessions`
```json
{
  "sessions": [
    {
      "name": "sessions/SESSION_ID",
      "id": "SESSION_ID",
      "state": "COMPLETED",
      "createTime": "2026-10-08T12:00:00Z"
    }
  ]
}
```

#### 3. `status`
```json
{
  "session": {
    "name": "sessions/SESSION_ID",
    "id": "SESSION_ID",
    "state": "IN_PROGRESS",
    "prompt": "Fix typo",
    "sourceContext": { "source": "sources/github/OWNER/REPO" }
  }
}
```
*(Executes exactly one GET request; does not scan activities).*

#### 4. `activities`
```json
{
  "sessionName": "sessions/SESSION_ID",
  "activities": [ ... ]
}
```
*(Fully paginates all activities).*

#### 5. `results`
```json
{
  "session": { ... },
  "classification": "completed",
  "outputs": [],
  "patches": [
    {
      "sessionName": "sessions/SESSION_ID",
      "source": "sources/github/OWNER/REPO",
      "activity": "sessions/SESSION_ID/activities/ACTIVITY_ID",
      "createTime": "2026-10-08T12:05:00.123456789Z",
      "artifactIndex": 0,
      "baseCommitId": "37c5a45885584831d8cbd6d9755a19d889e0b0be",
      "suggestedCommitMessage": "Fix typo in README",
      "patchSha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "patchAvailable": true,
      "applyBaseAvailable": true
    }
  ],
  "latestActivity": { ... },
  "delivery": "pr_reported"
}
```

- **Classification Mapping**:
  - `pending`: `QUEUED`, `PLANNING`, `IN_PROGRESS`
  - `blocked`: `PAUSED`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`
  - `failed`: `FAILED`
  - `completed`: `COMPLETED`
  - `unknown`: Any unrecognized state
- **Delivery Evaluation**: For `COMPLETED` sessions without a PR URL in the session payload, a single extra `GET /sessions/S` is performed. If still no PR, delivery is `completed_without_pr`; otherwise `pr_reported`.

#### 6. `pull --json`
```json
{
  "sessionName": "sessions/SESSION_ID",
  "source": "sources/github/OWNER/REPO",
  "activity": "sessions/SESSION_ID/activities/ACTIVITY_ID",
  "createTime": "2026-10-08T12:05:00.123456789Z",
  "artifactIndex": 0,
  "baseCommitId": "37c5a45885584831d8cbd6d9755a19d889e0b0be",
  "suggestedCommitMessage": "Fix typo",
  "patchSha256": "e3b0c44298fc...",
  "patch": "diff --git a/README.md b/README.md\n..."
}
```

#### 7. `pull --apply` & `teleport`
```json
{
  "sessionName": "sessions/SESSION_ID",
  "source": "sources/github/OWNER/REPO",
  "activity": "sessions/SESSION_ID/activities/ACTIVITY_ID",
  "artifactIndex": 0,
  "baseCommitId": "37c5a4588558...",
  "patchSha256": "e3b0c44298fc...",
  "cwd": "/path/to/repo",
  "branch": "octodot/SESSION_ID",
  "applied": true
}
```

#### 8. `reply`
```json
{
  "acknowledged": true,
  "dispatched": true,
  "operation": "sendMessage",
  "outcome": "acknowledged",
  "sessionName": "sessions/SESSION_ID"
}
```

#### 9. `approve-plan`
```json
{
  "acknowledged": true,
  "dispatched": true,
  "operation": "approvePlan",
  "outcome": "acknowledged",
  "sessionName": "sessions/SESSION_ID"
}
```

---

### Raw Pull Behavior (`pull` without `--json` and without `--apply`)

1. Emits selected artifact metadata to **stderr** as JSON.
2. Emits raw unidiff patch bytes directly to **stdout** without modifying newlines.
3. If the selected patch contains `JULES_API_KEY`, export is aborted with error `secret_in_artifact`.

---

### New Action Output (JSON Lines)

`python3 octodot.py -new ...` emits one attempt JSON line per requested ordinal in sequential order, followed by a single summary line:

```jsonl
{"type":"attempt","attempt":1,"outcome":"accepted","startedAt":"2026-10-08T12:00:00.123Z","fingerprint":"a1b2c3...","requested":{"repo":"OWNER/REPO","source":"sources/github/OWNER/REPO","branch":"main"},"observed":{"repo":"OWNER/REPO","source":"sources/github/OWNER/REPO","branch":"main"},"name":"sessions/123","id":"123","url":"https://jules.google/sessions/123","state":"QUEUED","prUrls":[],"contextVerified":true,"error":null}
{"type":"summary","requested":1,"accepted":1,"rejected":0,"uncertain":0,"createdUnverified":0,"createdContextMismatch":0,"notStarted":0,"ok":true,"exitCode":0,"error":null}
```

- **Attempt Outcomes**: `accepted`, `created_unverified`, `created_context_mismatch`, `uncertain`, `rejected`, `not_started`.
- **Summary Counters**: Exactly partition `requested` count across all outcomes.
- **Summary Error**: Selected from the lowest-numbered attempt with an error, or preflight error, or invocation stop error.

---

## 5. Concurrency & Parallel Dispatch

When `--parallel N` is requested ($1 \le N \le 100$):

1. **In-Flight Limit**: A `concurrent.futures.ThreadPoolExecutor` runs with `max_workers = min(5, N)`. At most 5 concurrent requests are in flight at any time.
2. **Batch Draining**: The main scheduler drains all currently completed futures before refilling slots with subsequent ordinals.
3. **Shared Stop Event**: If any attempt encounters an error, uncertainty, context mismatch, or deadline expiration, a shared `threading.Event` is set.
4. **Lane Cancellation**: Unsubmitted workers check the stop event and return `not_started` without performing network requests.
5. **No Replacements**: Failed or aborted lanes are never replaced with new attempts.

---

## 6. Deadlines, Timeouts & Retries

- **Admission Budget (`--deadline`)**: A monotonic clock deadline is established after argument parsing. Once remaining time $\le 0$, no new HTTP requests or Git commands are admitted.
- **Socket / Request Timeout (`--timeout`)**: Each HTTP request and Git subprocess timeout is bounded by $\min(\text{timeout}, \text{remaining})$.
- **GET Retries**: Up to 3 total attempts (initial + 2 retries) for HTTP 408, 429, 5xx, and transport errors. Delays are 1 second and 2 seconds, respecting `Retry-After` headers.
- **POST Retries**: **ZERO retries**. A POST request is made at most once.

---

## 7. Ambiguous Creation & Reconciliation

If a POST request fails due to:
- A socket or connection timeout
- Network exception or dropped TCP connection
- HTTP 408 (Request Timeout) or HTTP 5xx (Server Error)
- An HTTP redirect (3xx)
- Malformed JSON response or missing returned session name

The attempt outcome is classified as **`uncertain`** and the process terminates with **exit code 5**.

### Manual Reconciliation Steps

When exit code 5 occurs during creation:

1. **Do Not Recreate**: Never execute a fresh creation command immediately.
2. **List Sessions**: Run `python3 octodot.py -list-sessions`.
3. **Inspect Recent Sessions**: Identify sessions created near the `startedAt` timestamp recorded in the attempt receipt.
4. **Check Session Details**: Run `python3 octodot.py -status sessions/CANDIDATE_ID` and check the `prompt`, `title`, and `sourceContext`.
5. **Inspect Activities**: Run `python3 octodot.py -activities sessions/CANDIDATE_ID`.
6. **Decision**: Only if a session is confirmed to match the intended work, proceed with observing that session. If no matching session exists, explicitly authorize a fresh attempt.

---

## 8. Git Local Mutation Contract

Local mutations via `pull --apply` and `teleport` are restricted and guarded by strict preconditions:

### Preconditions

1. **Platform**: Must be running on Linux or macOS (Windows returns exit 3).
2. **Git Version**: Installed Git must be version $\ge 2.36$.
3. **Subprocess Sanitization**:
   - `GIT_TERMINAL_PROMPT=0`
   - `GIT_OPTIONAL_LOCKS=0`
   - `-c core.hooksPath=/dev/null`
   - `-c core.fsmonitor=false`
   - Stripped `GIT_DIR`, `GIT_WORK_TREE`, and index environment variables.
4. **Clean Worktree**:
   ```bash
   git status --porcelain=v1 -z --untracked-files=all --ignored=matching
   ```
   Must return zero entries. Any modified, staged, untracked, or ignored file causes immediate rejection.
5. **Base Commit Alignment**: Working tree `HEAD` must match the artifact's full 40- or 64-character `baseCommitId` exactly.
6. **Patch Preflight Inspection**:
   - Rejects any patch modifying `.gitmodules`.
   - Rejects mode changes (160000 gitlinks or 120000 symlinks).
   - Rejects directory traversal paths (`..`, absolute paths, leading slashes).
   - Rejects rename or copy patches (`rename from `, `rename to `, `copy from `, `copy to `, `similarity index `).
7. **Worktree-Only Guarantee**:
   - HEAD commit and `.git/index` file bytes are recorded before application.
   - Patch is applied using `git apply --whitespace=nowarn -`.
   - HEAD commit and index file bytes are re-verified after application to ensure the index was untouched.
