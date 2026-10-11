# octodot.py: Implementation Plan & Specification (Issue #3)

**Specification Title**: Replace controller with a single stateless octodot.py Jules API script<br>
**Issue Number**: #3<br>
**Author**: furylachori<br>
**Revision**: Revision 2 · 8 October 2026<br>
**Status**: Implemented Offline · Awaiting Authorized Live Execution


---

## Executive Summary & Review Status

All four independent adversarial reviewers rechecked the revised specification and reported zero confirmed unresolved findings. This document contains the complete authoritative specification for Issue #3 and the corresponding implementation evidence across the standalone runtime (`octodot.py`), offline verification suite (`test_octodot.py`), operational documentation, and CI workflows.

Offline gates are fully implemented and verified without network access. Live Jules creation, publication, and monitor activation retain their explicit authorization gates and have not been executed.

---

## 1. Selected Implementation and Evidence

### Specification Text

Implement one standalone `octodot.py`, using Python 3.10+ standard library and Git only for explicit local-repository operations. The selected implementation is the Jules REST API, authenticated by the existing `JULES_API_KEY`. The official CLI is deferred. There is no CLI-versus-Python gate or implementer choice.

This specification replaces the implementation appendix of the current [workflow plan](https://docs.google.com/document/d/1bHFPif9BnQW_UlPqdw-XTe_UIz_k9kUtK867qmjqHOI/edit), read 8 October 2026 after its 21:30 UTC Python-selection update. Preserve that document's one-shot workflow and authorization boundaries. Earlier local drafts that said to try the CLI first are superseded.

Verified evidence: existing API authentication successfully read sources and sessions in Codex. Native Jules v0.1.42 installation, login, and authenticated listing succeeded in one temporary HOME. Fresh-job CLI login persistence was not established. No assistant test has established API creation, actual patch retrieval, or create-to-PR success. The user's report that their CLI test had no approval pause is scoped user evidence, not a live API result.

Do not import or adapt the current controller. No SQLite, daemon, scheduler, grants, provider adapters, event journal, cache, background worker, persistent session mirror, or generic workflow engine. Authentication is environment input, not a login subsystem. A local Git clone requested by `-teleport` is a checkout, not a session store. No automatic old-session messages, plan approvals, patch application, PR retargeting, commits, pushes, merges, or deployments.

### Implementation Evidence

- **Single Runtime**: All runtime functionality is consolidated into `octodot.py`.
- **Zero Third-Party Dependencies**: Exclusively imports standard library modules (`sys`, `os`, `re`, `json`, `time`, `signal`, `urllib.request`, `urllib.error`, `urllib.parse`, `concurrent.futures`, `hashlib`, `subprocess`, `argparse`, `threading`, `datetime`).
- **No Persistence Subsystem**: No SQLite, JSON journal, or background daemons are used.
- **Stateless Execution**: Each CLI invocation executes to completion and terminates, printing results and receipts directly.

---

## 2. Repository Baseline and Exact Replacement Scope

### Specification Text

Repository: `furylachori/chori-octodot`. Read-only GitHub checks found `main` at commit `37c5a45885584831d8cbd6d9755a19d889e0b0be`. The current controller is already on main, despite README text claiming it is only on `impl/octodot-core`. Do not implement from the stale implementation branch.

When implementation is authorized, create local branch `impl/plain-octodot` from the freshly fetched main. Before editing, compare the current tree with this scope. If new unrelated paths or changes collide with this replacement, stop and report the conflicting paths; do not delete work by inference. A pre-existing implementation branch is a stop, not permission to reset it. Do not publish a branch or PR unless the execution authorization includes publication.

Final tracked files are exactly:

- `octodot.py`: sole runtime, executable shebang `#!/usr/bin/env python3`, mode 100755, version `1.0.0`.
- `test_octodot.py`: all offline tests and inline fixture builders; no external fixture files.
- `README.md`: quickstart, all supported commands, selected architecture, mutation warnings, limits, and truthfully separated verification status.
- `docs/IMPLEMENTATION_PLAN.md`: this specification, with actual completion evidence added after authorized implementation.
- `docs/OPERATIONS.md`: complete CLI, output, failures, reconciliation, and local-operation contract below.
- `docs/RELEASE_CHECKLIST.md`: exact offline and live gates below and their observed results.
- `.github/workflows/offline.yml`: offline CI below.
- `.gitignore`: retain existing exclusions unchanged; they remain useful for preventing accidental publication of secrets/old state.
- `docs/CODEX_WORKFLOW.md`: one-shot workflow and authorization guidance already retained in the current tree.
- `cloudbuild.yaml`, `cloudbuild-offline.yaml`, and `docs/CI.md`: the separately authorized, CI-only Cloud Build supplement described below. These files do not add runtime code, enable live Jules execution, or alter the existing GitHub Actions workflow.

Remove the tracked `src/`, `tests/`, `examples/`, `plan/`, and `schemas/` trees; `pyproject.toml`; `requirements-dev.txt`; `docs/AUTHORIZATION.md`; `docs/BASELINE.md`; and `docs/CONTRACTS.md`. This is a source-tree replacement recorded by Git, not erasure of history or runtime data. Never touch untracked files, user state directories, actual databases, credentials, or old installed copies. Do not add a package, installer, migration program, license, or compatibility entrypoint. Old `python -m octodot` and controller-plan interfaces are intentionally retired and documented as such.

### Implementation Evidence

- **Target Branch**: Work is isolated on `impl/plain-octodot`.
- **Original File Set**: The replacement specification named eight files; `docs/CODEX_WORKFLOW.md` was also retained in the current main tree and is part of its pre-supplement allowlist.
- **Current Exact File Set**: The separately authorized CI-only extension adds exactly `cloudbuild.yaml`, `cloudbuild-offline.yaml`, and `docs/CI.md`; the `ArchitectureTests` allowlist now covers all 12 tracked paths.
- **Scope Boundary**: These CI additions do not change the implementation plan's runtime, live-execution, or replacement scope.
- **Legacy Removal**: Staged deletions for `src/`, `tests/`, `examples/`, `plan/`, `schemas/`, `pyproject.toml`, `requirements-dev.txt`, `docs/AUTHORIZATION.md`, `docs/BASELINE.md`, and `docs/CONTRACTS.md`.
- **Architecture Validation**: `ArchitectureTests` in `test_octodot.py` verifies the tracked file allowlist, the absence of prohibited imports (such as `sqlite3` or third-party packages), and the absence of deprecated CLI entrypoints.

---

## 3. Exact CLI Grammar

### Specification Text

Use `argparse.ArgumentParser(allow_abbrev=False)`. Exactly one action is required, except standalone help/version. Every listed action has both single-dash and double-dash spelling: `-new/--new`, `-list-repos/--list-repos`, `-list-sessions/--list-sessions`, `-status/--status SESSION`, `-activities/--activities SESSION`, `-results/--results SESSION`, `-pull/--pull SESSION`, `-teleport/--teleport SESSION`. Help is `-h/--help`; version is `--version` only. Do not add unlisted flags. Duplicate occurrences of any option, including aliases, are usage errors; do not silently take the last value.

Supported invocations:

```
python3 octodot.py -new -prompt "instructions"
python3 octodot.py -new -prompt "instructions" --repo OWNER/REPO --branch BRANCH
python3 octodot.py -new --repo OWNER/REPO --branch BRANCH < instructions.txt
python3 octodot.py -new -prompt - --repo . < instructions.txt
python3 octodot.py -new -prompt "instructions" --repo OWNER/REPO --branch BRANCH --parallel 3 --title "Bounded task"
python3 octodot.py -list-repos
python3 octodot.py -list-sessions
python3 octodot.py -status SESSION
python3 octodot.py -activities SESSION
python3 octodot.py -results SESSION
python3 octodot.py -pull SESSION > change.patch
python3 octodot.py -pull SESSION --json
python3 octodot.py -pull SESSION --activity sessions/S/activities/A --artifact 0 --json
python3 octodot.py -pull SESSION --apply --cwd /absolute/checkout
python3 octodot.py -teleport SESSION --dir /absolute/new-directory --apply
```

The first invocation is supported by repository/branch inference; it is not a prompt-only global default.

Option applicability is strict:

- `-prompt/--prompt TEXT`, `--repo REPO`, `--branch BRANCH`, `--parallel N`, and `--title TEXT`: `new` only.
- `--json`: `pull` without `--apply` only. Other actions already return JSON.
- `--activity RESOURCE` and `--artifact INDEX`: `pull` and `teleport` only; both must appear together.
- `--apply`: `pull` or `teleport` only; mandatory for teleport.
- `--cwd DIR`: `new` when repository is inferred or `.`, or `pull --apply` only. It defaults to process cwd in these cases. Explicit OWNER/REPO plus `--cwd` is an error. `pull --cwd` without `--apply` is an error.
- `--dir DIR`: mandatory for teleport; invalid elsewhere.
- `--timeout SECONDS` and `--deadline SECONDS`: every API action. Defaults 30 and 120; finite positive decimal numbers only, reject NaN/infinity/zero/negative. No numeric maximum. Timing semantics are section 7.
- Help/version cannot combine with any other argument. Help/version require neither key, network, Git, nor cwd repository.

`--parallel` is the requested total number of alternative sessions, integer 1–100 inclusive, default 1. It does not divide a prompt into subtasks. At most five attempts are in flight. A total above five uses waves subject to the same invocation deadline. These are client bounds, not assertions about Google quotas. The routine correction workflow always supplies `--parallel 1`.

Prompt handling: a literal prompt must contain at least one non-whitespace character, but preserve its original whitespace and Unicode in the request. `-prompt -` reads UTF-8 stdin to EOF. Omitting prompt reads non-TTY stdin; omitted prompt on TTY is an error. Explicit `-prompt -` on TTY is an error rather than an interactive prompt. A literal prompt takes precedence over non-TTY stdin and never reads it; document this to avoid hanging when jobs have pipes. Duplicate prompt options are the only conflicting prompt-source error. Invalid UTF-8 or empty/whitespace-only input is exit 2 before network. Title, when supplied, must be nonempty after whitespace testing, preserved unchanged. No prompt/title is passed to a shell.

Session input: accept either one suffix matching `[A-Za-z0-9_-]+` or `sessions/` followed by that suffix; normalize to the resource name. Do not accept URLs, display titles, guessed display IDs, path traversal, or extra segments. Use returned resource names, not `id`, for subsequent requests. An unexpected name shape from the API is a protocol error, preserving any raw name in the sanitized receipt. Activity selector must match `sessions/S/activities/A` with the same suffix grammar, and its session must exactly equal the normalized requested session. Artifact index is a nonnegative decimal integer.

Repository input is `.` or exactly two nonempty components `OWNER/REPO`, each matching `[A-Za-z0-9_.-]+`, excluding `.` and `..`. Strip one trailing `.git` only when parsing a remote URL, not from an explicit repo name. Compare owner/repo case-insensitively; preserve API spelling for output. Branch names are nonempty and compared exactly with the source's branch list; no Git invocation is needed for explicit remote repo/branch.

Inference uses Git only: resolve the root via `git rev-parse --show-toplevel`; require exactly one origin fetch URL from `git remote get-url --all origin`. Accept only `https://github.com/OWNER/REPO[.git]`, `git@github.com:OWNER/REPO[.git]`, or `ssh://git@github.com/OWNER/REPO[.git]`, with an optional final slash. Reject URL credentials, ports, query/fragment, alternate hosts, multiple origins, and unsupported forms. If no branch was passed, use `git symbolic-ref --quiet --short HEAD`; detached HEAD is exit 2 requiring `--branch`. Do not infer from another remote. Explicit OWNER/REPO without branch uses the verified source's `defaultBranch.displayName`. Monitor jobs must always pass explicit repository and branch.

### Implementation Evidence

- **Grammar & Argument Parsing**: Handled in `parse_args` in `octodot.py` with custom duplicate-option detection and option compatibility checks.
- **Prompt Precedence & Stdin**: Implemented with strict non-TTY vs TTY checks, whitespace validation, and preserved Unicode.
- **Resource Normalization**: Regex validations ensure `sessions/{suffix}` and `sessions/{s}/activities/{a}` structure.
- **Git Inference**: `infer_repo` invokes Git with strict origin URL regexes, rejecting credentials, non-GitHub hosts, or multiple remotes.
- **Comprehensive Unit Tests**: `ParserTests` in `test_octodot.py` comprehensively exercises all valid and invalid option combinations, boundary cases, and stdin modes.

---

## 4. Authentication, REST Boundary, and Pagination

### Specification Text

Read exactly `JULES_API_KEY`, once, after local argument validation. Missing, empty, or whitespace-only is exit 3. Reject CR/LF in it as invalid configuration. Do not use `GJULES_API_KEY`, key files, command-line keys, login, logout, token copying, or credential creation. Never print credentials. Native Jules login/logout remain outside this program.

Fixed base: `https://jules.googleapis.com/v1alpha`. Requests use `X-Goog-Api-Key`, `Accept: application/json`, and, for POST only, `Content-Type: application/json; charset=utf-8`. Encode JSON as UTF-8. Use default TLS validation and existing system proxy settings. Disable all redirects, including same-origin redirects, so POST cannot be replayed and credentials cannot leak. No alternate URL option. Do not log headers, raw exceptions, proxy URLs, or raw non-JSON error bodies.

Endpoints are fixed:

- sources listing: `GET /sources?pageSize=100` with subsequent `pageToken`.
- source detail: `GET /{actual source.name}`.
- sessions listing: `GET /sessions?pageSize=100` with subsequent `pageToken`.
- session: `GET /sessions/S`.
- activities: `GET /sessions/S/activities?pageSize=100` with subsequent `pageToken`.
- selected activity detail: `GET /sessions/S/activities/A`.
- creation: `POST /sessions` only.

Source resource names are taken from Google, never constructed. Accept a `sources/` prefix followed by one or more slash-separated nonempty segments; reject dot/dot-dot segments, control characters, backslashes, query/fragment delimiters. Encode each segment with `urllib.parse.quote(segment, safe='')`, retaining separator slashes. Thus real slash-containing opaque source names work. Apply the same segment encoding to normalized session/activity paths. Query uses `urllib.parse.urlencode`; never splice page tokens into URLs.

Each list response must be a JSON object; a missing collection key means empty, a present non-list is a protocol error. Entries must be objects with valid unique names for that resource family. Repeated names across pages make the scan incomplete/protocol-failed rather than silently hiding changing data. `nextPageToken` must be missing/empty or a string. Continue through empty pages with nonempty tokens. Track seen tokens and reject repetitions. No undocumented filter, timestamp window, page limit, or assumed order. On failure preserve accumulated data, mark `complete:false`, and return nonzero. Partial scans never prove absence. A fully paginated scan is complete under the API contract; it is not a transactionally frozen snapshot.

Resolve a creation source by full sources scan, exact case-insensitive owner/repo match, and then GET of that source. Zero/multiple matches, malformed/missing GitHub identity, wrong returned name, changed repo identity, absent branches/default branch, or missing requested branch stops before POST. Detail branch availability is union of nonempty `branches[].displayName` and the nonempty defaultBranch displayName; accepting the default branch does not require its duplicate presence in branches. A non-default branch must be explicitly listed. No connecting repos, switching repos, fallback to main, or blind constructed source identifier.

### Implementation Evidence

- **Authentication**: `JULES_API_KEY` is checked once via `os.environ.get("JULES_API_KEY")`, validated for absence of whitespace/CR/LF.
- **Redirects Disabled**: Custom `NoRedirectHandler` subclassing `urllib.request.HTTPRedirectHandler` returns an error for any redirect response code (301, 302, 303, 307, 308).
- **Segment URL Encoding**: `urllib.parse.quote(seg, safe='')` is applied to each resource segment.
- **Pagination**: Implemented in `paginate` function with seen token sets, duplicate name detection, and non-list error handling.
- **Verification Suites**: Tested in `TransportTests`, `PaginationTests`, and `SourceTests`.

---

## 5. Output and Exits

### Specification Text

UTF-8 JSON uses `ensure_ascii=False`, sorted keys, compact separators, and exactly one trailing newline; no terminal color. All output values are recursively redacted if they contain the exact key string. Other returned private data stays in user-requested results. Errors contain only fixed client messages plus sanitized Google `error.code`, `error.status`, and `error.message`; omit raw bodies/exception strings. Truncate provider messages to 2048 characters after redaction. Progress/receipts/errors are JSON lines on stderr, flushed after every line. Use one shared threading.Lock covering the complete serialized line write and flush for every stdout/stderr JSON emission, including worker receipts; do not allow interleaved output. No traceback unless developer edits code; there is no debug flag.

Every non-new JSON command returns one object with keys `action`, `ok`, `complete`, `data`, and `error`. `error` is null on success, otherwise `{kind,message,httpStatus,operation,provider}`. kind/message/operation are strings, httpStatus is integer or null. provider is null unless a JSON Google error object exists, then `{code,status,message}`: code is integer or null, status and message are strings or null; wrong-typed fields become null. Provider message uses the redaction and 2048-character limit above; client message remains fixed. `ok` is transport/operation success, not Jules task success. `complete` means all required reads/steps succeeded. Local-only parser errors produce the same error envelope on stderr, no stdout, exit 2. Other command failures return the envelope on stdout and the error object on stderr. Raw pull is the exception: before output begins, any failure leaves stdout empty and emits only a stderr error envelope. An output I/O failure after writing begins can leave a truncated patch stream; report it when stderr remains writable, exit 4, and do not pretend stdout can be retracted.

Data shapes:

- `list-repos`: `{sources:[raw Source objects]}`.
- `list-sessions`: `{sessions:[raw Session objects]}`.
- `status`: `{session:raw Session}`; only one GET, no hidden activity scan.
- `activities`: `{sessionName,activities:[raw Activity objects]}`; full scan.
- `results`: `{session:raw Session,classification,outputs,patches,latestActivity,delivery}`. `outputs` defaults to `[]`; `patches` metadata below. It fetches session, source, then complete activities. Classification is `pending` for QUEUED/PLANNING/IN_PROGRESS; `blocked` for PAUSED/AWAITING_PLAN_APPROVAL/AWAITING_USER_FEEDBACK; `failed` for FAILED; `completed` for COMPLETED; otherwise `unknown`, preserving raw state. latestActivity is the raw Activity object for the unique newest valid timestamped activity, or null when ambiguous, without inventing a reason. patches preserves full-list encounter order, then ascending original artifact index within each activity. For COMPLETED with no output PR URL, perform one additional fresh session GET after the activities scan. If still no PR, delivery is `completed_without_pr`; otherwise `pr_reported`. Other delivery values are `pending`, `blocked`, `failed`, or `unknown` matching classification. A failed extra read is incomplete, not proof of nondelivery. Remote failure/block/no PR remains exit 0 when all reads succeed; acceptance separately fails/blocks.
- `pull --json`: `{sessionName,source,activity,createTime,artifactIndex,baseCommitId,suggestedCommitMessage,patchSha256,patch}`.
- `pull --apply` and teleport: `{sessionName,source,activity,artifactIndex,baseCommitId,patchSha256,cwd,branch,applied:true}`. On mutation failure `data` retains selected metadata and stage plus `applied:false` or `applied:null` when outcome is uncertain; never claim rollback.

If selected patch text contains the exact API key, refuse export/application as `secret_in_artifact` rather than redact or transmit that patch. Otherwise raw pull outputs exactly `unidiffPatch.encode('utf-8')`, no newline added/removed and no metadata mixed in. Emit the chosen metadata without patch to stderr before writing bytes. Buffer and validate the full chosen artifact before stdout. Broken pipe exits 4; for new output failure, stop submitting and warn that emitted receipts may be incomplete.

New emits one stdout JSON line per requested ordinal, in ordinal order after in-flight work drains, followed by exactly one summary line. Immediate receipts still go to stderr as they occur. Attempt object keys: `type:"attempt"`, `attempt` (1-based), `outcome`, `startedAt`, `fingerprint`, `requested:{repo,source,branch}`, `observed:{repo,source,branch}`, `name`, `id`, `url`, `state`, `prUrls`, `contextVerified`, `error`. Fields never observed are null, URLs are returned values only, absent PRs are `[]`. Outcomes are `accepted`, `created_unverified`, `created_context_mismatch`, `uncertain`, `rejected`, `not_started`. `contextVerified` is true only on exact source and branch verification; false for mismatch, null if unavailable/not sent. Preflight failure prints every ordinal as not_started plus summary with the actual preflight error. Do not treat not_started as rejected.

Summary keys: `type:"summary"`, `requested`, `accepted`, `rejected`, `uncertain`, `createdUnverified`, `createdContextMismatch`, `notStarted`, `ok`, `exitCode`, `error`. Counts cover every ordinal exactly once. summary.error is the non-null error from the lowest-numbered attempt that has one; otherwise it is the preflight error, then the invocation stop error, then null, in that order. `ok:true` requires every ordinal accepted and no invocation-level error. `accepted` means accepted/context-verified, not completed or a delivered PR.

Exit codes with precedence: 5 for any uncertain/known-created-unverified/context-mismatch; else 3 for missing configuration or HTTP 401/403 authentication/access gate; else 4 for transport, protocol, quota, provider, local Git, deadline, or any other failed/not-started operation; else 0. Invalid local input is 2 before remote work. HTTP 403 is reported as access/configuration failure without assuming invalid key. Install SIGINT/SIGTERM handlers in main before argument parsing, for all actions/stages including stdin/preflight/reads: record fixed `interrupted` stop error, set the shared stop event, stop admission, and drain admitted workers. Interruption gives at least exit 4 even if all already-submitted attempts become accepted; exit 5 or 3 still wins under the stated precedence. Clean SIGINT/SIGTERM after any submitted POST is handled by stopping refill and draining workers; if the process cannot drain, its transcript remains uncertain. Shell-forced termination exit codes are external, not these application results.

### Implementation Evidence

- **Output Serialization**: `emit_json` implements UTF-8, `sort_keys=True`, compact separators, line-buffered writing with a global `threading.Lock`.
- **Recursive Redaction**: Scans strings, lists, dictionaries, and error messages for occurrences of the active key.
- **Envelopes**: Exact shapes verified for `list-repos`, `list-sessions`, `status`, `activities`, `results`, `pull --json`, `pull --apply`, and `teleport`.
- **Signal Handlers**: Installed early in `main` for SIGINT and SIGTERM, initiating graceful stop and worker drain.
- **Exit Code Precedence**: Implemented via explicit precedence evaluation logic ($130 > 5 > 3 > 4 > 2 > 0$).
- **Verified in Tests**: Fully exercised by `OutputTests` in `test_octodot.py`.

---

## 6. Exact Creation and Parallel Dispatch

### Specification Text

Construct only this JSON body (omit title if absent):

```
{"prompt":PROMPT,"sourceContext":{"source":SOURCE_NAME,"githubRepoContext":{"startingBranch":BRANCH}},"requirePlanApproval":false,"automationMode":"AUTO_CREATE_PR","title":TITLE}
```

Never send requestId, PR-base, draft, immutable-SHA, or unknown fields. `requirePlanApproval` and automationMode are input-only; do not demand that GET echoes them. Jules can still plan, ask questions, fail, or produce no PR.

Preflight once per invocation, then canonicalize body with the output JSON settings and SHA-256 its UTF-8 bytes. Before every POST emit and flush `{type:"create_started",attempt,startedAt,repo,source,branch,fingerprint}` to stderr. startedAt is UTC RFC3339 with milliseconds. Fingerprint is evidence, never an idempotency key. If the diagnostic write fails, do not POST.

Every admitted attempt makes at most one POST. Once a 2xx JSON object with a valid session name arrives, emit/flush `{type:"create_accepted",attempt,name,id,url,startedAt,fingerprint}` before any further request. If this accepted-receipt write/flush fails, retain the known returned resource in memory as `created_unverified`, stop refill, do not start its verification GET, and emit the final known receipt only if output remains writable; never relabel it uncertain creation. Compare returned source and startingBranch with requested values. If both exist and exactly match, accept without another GET. A present mismatch is `created_context_mismatch`, not something to fix. If either is missing, GET that exact name once using the ordinary bounded GET retry policy and verify. Missing/mismatched context or failure after known creation retains the receipt and is exit 5. Do not construct a substitute name from id.

Any POST transport exception, timeout, HTTP 408, HTTP 5xx, redirect, malformed success body, or missing/invalid returned name is `uncertain`; never retry automatically. Other HTTP 4xx, including 429, are `rejected`, no retry. A request rejected locally before transport is not_started. An HTTP 2xx valid name followed by any later failure is known-created, not uncertain creation. Preserve raw returned name as sanitized diagnostic evidence when its shape is invalid, but never GET it.

Use `ThreadPoolExecutor(max_workers=min(5,N))`; do not enqueue more than five futures. A main-thread scheduler drains ALL currently completed futures before refilling. Any rejected, uncertain, unverified, mismatch, deadline, interruption, or output-failure outcome sets a shared stop event. Workers check stop/deadline immediately before emitting create_started and again before transport; an unsubmitted lane returns not_started. Already sent calls drain and retain receipts. Never submit replacements for a failed lane. Refills use ascending ordinals only. Stop-event observations reduce races but cannot unsend already admitted calls; do not promise that a sibling rejection prevents every simultaneous POST. Worker-local read/POST operations remain sequential, so at most five HTTP calls run concurrently. Main preflight is finished before workers start.

After ambiguous creation, the coordinating assistant must reconcile via complete remote session listing and detailed candidate reads, comparing actual prompt, source, branch, title, createTime, and the transcript. Input-only fields are not reconstructible from GET. A matching time/fingerprint alone is not proof. A partial scan never licenses recreation. If evidence does not identify one existing session confidently, keep the attempt uncertain and obtain a decision before any fresh creation. No persistent local receipt file or automatic reconciliation loop is added.

### Implementation Evidence

- **Body Construction**: Matches the exact schema without extraneous fields.
- **Fingerprinting**: Canonical JSON representation hashed with SHA-256.
- **Zero POST Retries**: Enforced in `create_one`; POST is executed at most once per attempt.
- **Bounded Concurrency**: `create_many` manages a queue with a maximum of 5 in-flight futures, draining completed tasks before refilling with subsequent ordinals.
- **Shared Stop Event**: Any error or interruption sets `threading.Event`, preventing submission of subsequent attempts.
- **Verification Suites**: `CreateTests` and `ParallelTests` verify concurrency limits, receipt ordering, and exit codes.

---

## 7. Time and Retry Contract

### Specification Text

Start one `time.monotonic()` deadline after argument parsing and before prompt reading/key/preflight. It is an admission budget, not a process-kill guarantee. Do not begin any HTTP or Git operation or retry once remaining time is nonpositive. Each admitted HTTP/socket timeout and Git subprocess timeout is `min(timeout, remaining)`. Prompt stdin reading and OS DNS/socket behavior may outlive a soft deadline; recheck afterward. Running futures drain; no claim of hard 120-second completion. No optional outer timeout dependency is introduced by this implementation.

GET has at most three total attempts: first plus two retries. Retry only transport errors, HTTP 408/429, and HTTP 500–599. TLS certificate failures, redirects, malformed successful JSON, schema errors, and all other 4xx are not retried. First delay is one second, second two seconds. Parse Retry-After as nonnegative integer seconds or valid HTTP-date; use max(base delay, server delay). Invalid header falls back to base delay. If full delay would consume the remaining budget, stop with deadline error; do not sleep less and retry earlier than the server asked. No jitter. Each retry rechecks admission. No POST retries under any condition. Timeout failure after Git mutation is reported potentially partial; do not repeat Git mutation.

### Implementation Evidence

- **Admission Deadline**: Started via `time.monotonic() + args.deadline` and passed to all transport and subprocess calls.
- **Bounded GET Retries**: Up to 3 attempts with delays of 1s and 2s, respecting `Retry-After` header values.
- **Strict Non-Retry of POST**: POST calls have zero retries.
- **Verified in Tests**: Tested in `TransportTests` and `ParallelTests`.

---

## 8. Artifacts and Deterministic Selection

### Specification Text

The API artifact fields used are `activity.artifacts[index].changeSet.source` and `changeSet.gitPatch.{baseCommitId,unidiffPatch,suggestedCommitMessage}`.

For results/pull/teleport, GET session and the session's returned source; verify names and GitHub repo identity, then collect ALL activities. Candidate metadata keeps original array indices. A candidate is a changeSet with source equal to session.sourceContext.source and gitPatch object. Each inventory object has exactly `{sessionName,source,activity,createTime,artifactIndex,baseCommitId,suggestedCommitMessage,patchSha256,patchAvailable,applyBaseAvailable}`. sessionName/source/activity are verified strings; artifactIndex is the original integer index. Missing or wrong-typed optional text fields are null. patchSha256 is the SHA-256 of UTF-8 patch bytes only when patchAvailable is true, otherwise null. patchAvailable means nonempty string patch without the exact API key; applyBaseAvailable means baseCommitId is a full 40- or 64-hex string. These are separate: a missing base does not prevent export. Include unusable candidates with these false flags rather than conceal their existence. Invalid timestamps remain raw strings in inventory but block automatic selection. Nonmatching sources are excluded; a specifically selected wrong-source artifact is an error.

For an explicit selector, find its activity in the complete listing and GET that exact activity to obtain current full contents. Validate name and session namespace again; select the original artifacts array index; require matching source, nonempty string patch, and gitPatch. For automatic selection, consider all matching-source gitPatch candidates, including those without usable patch text. Require every candidate's createTime to be valid RFC3339; compare instants at full nanosecond precision (parse numeric fraction to nine digits and UTC offset; do not lose nanoseconds through microsecond datetime truncation). Take the unique candidate at maximum time. Any missing/invalid candidate timestamp or tie across latest candidates yields `ambiguous_patch` with candidate metadata; caller must supply both selectors. If the selected latest candidate has empty/missing patch, return `no_patch_available`; do not fall back to an older artifact. GET the chosen activity once before output/application and verify identity/source/index/patch fields again. Compare timestamp, source, original index, baseCommitId, exact unidiffPatch, and suggestedCommitMessage with the listed snapshot. Any difference, including a same-timestamp payload/base change, stops `artifact_changed`; no silent reselection. If there are no matching candidates, return no_patch_available.

Raw and JSON pull allow absent baseCommitId (null metadata); local apply requires a full 40- or 64-hex commit ID. Preserve patch text exactly. Compute patch SHA-256 from UTF-8 bytes. Never concatenate patches or claim the latest available artifact equals a final PR diff. Results never run artifact shell commands, download media, or interpret suggestedCommitMessage as instructions.

### Implementation Evidence

- **Artifact Extraction**: `collect_patches` parses all activity artifacts and produces candidate inventory dictionaries.
- **Nanosecond Ordering**: Timestamps parsed using custom fractional-second logic to prevent microsecond truncation.
- **Fresh Activity GET**: Selected activity is fetched freshly before patch emission to detect `artifact_changed`.
- **Secret in Artifact**: Any patch containing the API key is refused with `secret_in_artifact`.
- **Verified in Tests**: `ArtifactTests` in `test_octodot.py`.

---

## 9. Explicit Apply and Teleport

### Specification Text

These actions are opt-in local mutations. The normal fresh-session workflow never calls them. Git is invoked through argument arrays, `shell=False`, captured output, and bounded subprocess timeout; no dynamic shell snippets. Restrict supported local OSes to Linux and macOS; remote read/create actions remain Python-portable. Windows local mutation reports unsupported-platform exit 3.

All Git calls set `GIT_TERMINAL_PROMPT=0`, `GIT_OPTIONAL_LOCKS=0`, and `-c core.hooksPath=/dev/null -c core.fsmonitor=false`. Require installed Git >=2.36 for local operations so boolean fsmonitor disabling has supported semantics; older Git is an exit-3 configuration gate before repository operations. Remove inherited `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, and `GIT_ALTERNATE_OBJECT_DIRECTORIES` for subprocesses, so the selected cwd controls the target. Keep existing ordinary credential helpers; do not install/configure credentials or initiate login. Reject configured `url.*.insteadOf`/`pushInsteadOf` rewrites and any nonempty `filter.*.(clean|smudge|process)` command before clone/checkout/application. These are deliberate v1 capability stops, not attempts to sandbox arbitrary user Git configuration. Never execute source files, hooks, tests, or artifact bash commands as part of local patch operations.

For `pull --apply`:

1. Resolve cwd to its canonical path and require it exists. Resolve Git top-level and require cwd equals that canonical top-level, preventing Git's subdirectory patch omission. Reject bare repos, sparse checkouts, unresolved merges, any tracked gitlink/submodule, any tracked symlink, and index entries with skip-worktree or assume-unchanged flags. Reject an untracked nested `.git` file/directory detected while walking worktree, excluding root `.git`. These simple restrictions are intentional. Local apply/teleport are ancillary bounded v1 conveniences, not full native Jules CLI parity; unsupported rename/copy/submodule/symlink cases remain exportable read-only when artifact selection succeeds.
2. Require single origin URL normalized by section 3 equals authenticated source owner/repo. Read-only pull never needs this or Git.
3. Require `git status --porcelain=v1 -z --untracked-files=all --ignored=matching` to be empty, including ignored files, apart from root `.git` managed by Git. No resets, stashes, ignored-file deletion, or automatic cleanup. Ignore settings must not hide local data.
4. Verify `git rev-parse --verify HEAD` equals selected full baseCommitId case-insensitively. Do not fetch/reset/change branch to repair apply's base. Require same object-ID length.
5. Preflight patch structure via `git apply --numstat -z -` and `git apply --summary -`; inspect affected ordinary paths from NUL output. V1 refuses every rename/copy patch before mutation: reject a patch containing an extended-header line beginning `rename from `, `rename to `, `copy from `, `copy to `, or `similarity index `. This conservative line-based rejection may reject unusual text content; it must never interpret summary output as a machine-safe path list. Do not claim git apply --numstat includes both rename paths. Reject absolute paths, `..`, empty components, `.git` components case-insensitively, symlink parents, gitlink/symlink mode changes (160000/120000), and any touched path resolving outside root. Also reject `.gitmodules` edits. Non-text/binary patches are supported only if Git parses and checks them normally; no custom patch rewriting. Git's own default unsafe-path protection stays enabled.
6. Run `git apply --check --whitespace=nowarn -` using identical patch bytes. Then repeat clean/base checks immediately before `git apply --whitespace=nowarn -`. This applies to worktree only; do not use `--index`, `--cached`, `--3way`, `--reject`, `--unsafe-paths`, or `--allow-empty`.
7. Before applying, capture HEAD and the raw bytes of the actual index file reported by `git rev-parse --git-path index`; a missing index is a preflight failure. Successful apply must leave HEAD and those index bytes unchanged; verify both against pre-apply values without `git write-tree` or any index-writing command. Return selected metadata and modified cwd. If Git fails/times out or post-verification differs, report failure and possible partial state, leave files in place, and do not claim rollback. Concurrent external writers are unsupported; repeated checks mitigate but cannot eliminate TOCTOU.

For `teleport --dir ABSENT --apply`:

- Resolve parent canonical path; require an existing writable parent and a final directory name that does not exist, including broken symlinks (`lexists`). Never create parent directories or overwrite a destination.
- Complete session/source/patch selection before creating local files. Use authenticated source to form `https://github.com/OWNER/REPO.git`; no credentials embedded.
- Require Git configuration preflight above, then `git clone --no-checkout --no-recurse-submodules --template= -- https://github.com/OWNER/REPO.git ABSOLUTE_DIR`. No shallow or single-branch options. Existing auth only; failure leaves partial directory, with no cleanup/retry.
- Check full selected base exists as a commit with `git cat-file -t BASE` exactly `commit`; do not fetch an arbitrary SHA if absent. Inspect its tree for symlinks/gitlinks and reject before checkout under the same v1 restrictions.
- Create local branch `octodot/` plus the normalized session suffix using `git checkout -b BRANCH BASE --`. This is a local branch only. Branch characters are already constrained. Record source startingBranch as provenance, not a claim that remote branch is frozen.
- Execute the same apply checks and operation from repository root. Return actual directory/branch on success or failure. Do not push, commit, merge, delete a partial clone, alter the old PR, or guess a Jules branch. A clone/checkout is expected filesystem mutation, not code execution.

### Implementation Evidence

- **Subprocess Security**: Standardized in `run_git` with sanitized environment variables, explicit argument lists, and disabled hooks/fsmonitor.
- **Git Version Check**: Requires Git $\ge 2.36$, returning exit 3 if older.
- **Preflight Validations**: Working tree cleanliness checked with `-z --untracked-files=all --ignored=matching`. HEAD and index files recorded and verified byte-for-byte post-apply.
- **Patch Inspection**: Rejects renames, copies, mode changes (symlinks/gitlinks), `.gitmodules` modifications, and paths with traversal components.
- **Teleport Logic**: Validates destination via `os.path.lexists`, executes clone, checkout of `octodot/SESSION`, and applies the verified patch.
- **Verified in Tests**: `GitTests` in `test_octodot.py`.

---

## 10. Function Layout and Implementation Sequence

### Specification Text

One runtime module containing constants plus flat functions: `parse_args`, `emit_json`, `error_record`, `request_json`, `paginate`, `run_git`, `infer_repo`, `resolve_source`, `create_one`, `create_many`, `read_session`, `read_activities`, `classify_session`, `collect_patches`, `select_patch`, `apply_patch`, `teleport`, and `main`. Small internal helpers for validation/time parsing/redaction are permitted in that same file. No application/controller classes. A tiny urllib redirect-handler subclass and a private exception carrying sanitized errors are permitted solely because their libraries require that mechanism; they are not architectural layers. Tests patch the HTTP boundary, clock, sleep, and Git subprocess boundary as needed. No dependency injection framework.

Implement in this order; do not start live mutation at any step:

1. Replace tree and docs scaffolding; implement strict grammar, version/help, standardized output, validation, and redaction. Gate: ParserTests, OutputTests, ArchitectureTests pass.
2. Implement HTTP, deadlines/retries, resource validation/pagination, source/inference, and read actions. Gate: TransportTests, PaginationTests, SourceTests, ReadTests pass.
3. Implement exact POST receipts/context verification and bounded parallel scheduler. Gate: CreateTests and ParallelTests pass with no real network.
4. Implement artifacts, exact bytes, apply/teleport. Gate: ArtifactTests and GitTests pass, using disposable local fixtures only.
5. Rewrite complete usage/operations/checklist and CI; run all offline commands. Gate: all named suites plus aggregate pass, zero skipped tests, architecture allowlist exact, no external dependencies.
6. Obtain and execute the bounded live authorization of section 12. This is a separate gate, not something an implementer invents to finish step 5.
7. Publish only if authorized; verify remote commit and CI on that exact SHA. Do not activate monitors or merge. Final report distinguishes local implementation, published status, offline tests, live submission, live acceptance, and migration separately.

### Implementation Evidence

- **Flat Module Structure**: Implemented directly in `octodot.py` with flat functions and minimal private helpers.
- **Sequencing**: Steps 1–5 completed offline. Steps 6–7 pending explicit live authorization.

---

## 11. Exact Offline Verification

### Specification Text

Tests use `unittest`, `unittest.mock`, `tempfile`, stdlib HTTP/JSON fixtures, and installed Git. All network access is mocked/forbidden in the default run. Set a global test guard on socket connection creation to fail if a test attempts network. No Jules/GitHub credentials needed; test env uses a sentinel secret solely to prove redaction. Git fixtures set user name/email locally to synthetic test values and commit only inside temporary local fixture repositories; they never push. Mock teleport's network clone to a local fixture clone at the subprocess boundary, while asserting production argv uses the verified HTTPS GitHub URL.

Exactly these unittest classes exist in `test_octodot.py`:

- `ParserTests`
- `OutputTests`
- `ArchitectureTests`
- `TransportTests`
- `PaginationTests`
- `SourceTests`
- `ReadTests`
- `CreateTests`
- `ParallelTests`
- `ArtifactTests`
- `GitTests`

Execution sequence:

```bash
python3 --version
git --version
python3 -m py_compile octodot.py test_octodot.py
python3 octodot.py --help
python3 octodot.py --version
python3 -m unittest -v test_octodot.ParserTests test_octodot.OutputTests test_octodot.ArchitectureTests
python3 -m unittest -v test_octodot.TransportTests test_octodot.PaginationTests test_octodot.SourceTests test_octodot.ReadTests
python3 -m unittest -v test_octodot.CreateTests test_octodot.ParallelTests
python3 -m unittest -v test_octodot.ArtifactTests test_octodot.GitTests
python3 -m unittest -v test_octodot
git diff --check
```

### Implementation Evidence

- **Test Suite Structure**: All 11 classes implemented in `test_octodot.py`.
- **Global Network Guard**: `socket.socket.connect` patched globally during test execution to reject any network attempt.
- **CI Matrix**: Configured in `.github/workflows/offline.yml` across Ubuntu and macOS on Python 3.10–3.13 without dependencies or secrets.
- **Cloud Build Profiles**: The separately authorized `cloudbuild.yaml` runs the quick offline verification subset; `cloudbuild-offline.yaml` runs the complete `test_octodot` suite with its global network guard. Both use Python 3.13.7, bounded timeouts, Cloud Logging only, and no secrets. The GitHub Actions matrix above remains intact.

---

## 12. Live Acceptance and Activation

### Specification Text

Offline implementation can finish without live creation. Live acceptance is blocked until the user/coordinating assistant supplies an explicitly approved tuple: environment, repository, existing starting branch, exact bounded task/prompt, expected PR base, and permission for ONE Jules session plus automatic PR publication. For a correction test also supply original PR URL, head repository/branch, reviewed head SHA, and confirmed findings. These are task/authority inputs, not architecture choices. No task, branch, synthetic benchmark, or spending authorization is invented by the implementer.

When that tuple exists:

1. In one fresh bounded Luna Codex job verify Python 3.10+, script version, `-list-repos` complete/authenticated result, actual source, and exact existing branch. Existing key is used without displaying it. Failure stops before POST.
2. For a correction, before any POST require tuple repository = original PR head.repo.full_name, tuple starting branch = original PR head.ref, and approved expected PR base = that same head branch. If any equality fails, stop without consuming the one-session authorization. Re-read original PR head repository, branch, and SHA immediately before dispatch; any changed identity or SHA stops for new review. Pass exact repo/branch and `--parallel 1 --timeout 30 --deadline 120`, complete prompt on stdin, and no apply/teleport. Include original head SHA in prompt and require Jules to verify it before editing. The API selects a branch, not a pinned commit; acknowledge the remaining race.
3. Execute exactly one new call; retain pre-POST evidence and returned receipt in the ordinary job transcript. If accepted, read `-status` for that exact returned name and verify source/branch. Do not turn successful submission into end-to-end pass. Uncertainty invokes section 6 reconciliation, never a blind rerun.
4. Parent observes the same session. Before monitor activation, perform direct read-only checks every 60 seconds for this authorized acceptance; after activation, use the existing approximately 30-minute monitor instead, with no duplicate observation job. PR delivery transitions observation to exact-head review/CI verification; continue read-only checks at the same cadence while that verdict is pending, without creating another session. Stop at final live_passed/live_failed/live_blocked verdict, FAILED, blocked/question/approval state, completed-without-PR after prescribed fresh read, explicit user stop, or loss of access requiring user input. Pending remains pending and never creates a replacement. Do not send old-session feedback to force completion. A bounded Luna job ends after submission; parent owns later observation.
5. For an ordinary task, verify returned PR repository/base equal approved expected repository/base, new head is distinct, and diff fulfills task. For corrections, require new PR repository equals original head repository, new PR base.ref equals original head branch, correction head is distinct, and reviewed original head SHA is an ancestor of correction head. Require a reviewer and an independent challenger to inspect the exact original SHA-to-correction SHA diff, confirm every supplied finding is fixed, and report no confirmed new blocker. Record both SHAs and actual PR URL. For the exact tested correction SHA, collect complete paginated CI evidence and inspect the latest check run per (app identity, check name) and latest commit status per context for that SHA, using GitHub list-check-runs-for-ref with documented `filter=latest` across all pages, plus the combined commit-status endpoint’s current contexts across all pages. Duplicate app/name results that cannot be established as one latest result, missing required fields, or otherwise ambiguous evidence are live_blocked. Do not require a nonexistent check-run created_at field or guess chronology from IDs. Superseded runs/statuses are history and do not override a later result. For those effective checks/statuses: any queued/in-progress/pending is live_pending; any failure/error/timed_out/cancelled/action_required/startup_failure/stale conclusion is live_failed; inability to obtain complete check/status evidence is live_blocked; success/neutral/skipped are acceptable only when each conclusion is recorded without calling skipped checks passed. A repository with no reported checks must explicitly record no checks configured/reported; it is not called green. Any unresolved confirmed review finding is live_failed. Re-read PR head SHA immediately before final verdict; if changed, discard prior review/CI verdict and repeat on the new head rather than finalize stale evidence. Do not claim ancestry from branch names alone. GitHub comparison/read evidence must establish it.
6. Jules has no documented independent PR-base field. Therefore ANY mismatched base/repo, inaccessible fork/source/branch, missing required ancestry, noncorrection changes, or unverified relationship is `blocked_topology`; report actual PR and stop. No retarget/merge/rebase/close/fallback to main. If original head advanced after dispatch, report concurrent change and require re-review rather than claim it still matches.
7. Fetch real activities/results and `-pull --json` read-only from that same session, demonstrating artifact provenance and usable patch bytes. No real local apply/teleport acceptance is implied. Their live proof requires separate approval naming disposable destination and action; offline Git fixtures already gate implementation.
8. Record the actual outcome as live_passed, live_failed, live_blocked, live_pending, or live_not_run. Only live_passed means one authorized session delivered the intended verified PR and real patch. A draft PR is not required by this spec because API has no draft field; if the approved task explicitly requires draft, prompt for it and treat actual nondraft as a blocked mismatch without changing it.

Automation migration needs the user's separate final workflow instruction after validation. Preserve current monitor/reviewer/challenger work until then. Migration replaces old-session fix-triggering review feedback with a fresh Luna task and fresh Jules session; do not trigger both for the same findings. Existing review-posting/@jules authority does not itself authorize new coding sessions. Because plain PR feedback can trigger Jules outside Reactive Mode, do not retain old-PR feedback in the activated fresh-session path unless an existing verified no-auto-feedback/Reactive setting plus nontriggering content makes it safe. Do not change provider settings without authority. Jules CI Fixer can independently push new fixes without feedback comments; Reactive Mode does not disable that behavior. One-shot means one client submission, not one immutable provider patch. This workflow does not promise old-session inactivity and always pins review/CI to exact SHAs and rechecks them. No automated merge/deployment/integration of stacked PRs.

Parent serializes dispatch decisions and compares original PR + reviewed SHA + confirmed findings against current transcript receipt/session. Any existing receipt for the same original PR + reviewed SHA + normalized confirmed findings suppresses duplicate dispatch, whether pending, delivered but unintegrated, blocked, failed, or uncertain. Normalize confirmed findings as sorted unique tuples of repository-relative path, affected symbol/line range, and factual defect description; cosmetic wording differences do not establish new work and ambiguous equivalence stops for parent review. An explicit authorized retry is required for the same work after a blocked/failed attempt, with uncertain creation reconciled first. Missing/incomplete transcript evidence stops dispatch instead of assuming no receipt. A genuinely new correction pass requires fresh reviewed changes or distinct confirmed findings, a complete updated prompt, a fresh Luna job, and a fresh session. The script is stateless and provides no cross-process exactly-once guarantee.

### Implementation Evidence

- **Status**: Section 12 represents live acceptance gates that remain **pending explicit authorization inputs**.
- **No Synthetic Creation**: No live API creation has been attempted or simulated during offline development.
- **Operational Procedures**: Documented in detail in `docs/RELEASE_CHECKLIST.md`.

---

## 13. Review Gates and Completion Report

### Specification Text

Four independent adversarial reviewers examine this specification: (1) API/auth/resources/errors; (2) CLI/output/concurrency; (3) local Git/artifacts/security; (4) workflow/replacement/tests/authorization. Each reports concrete violated requirements with section and counterexample, not speculative redesign. Correct confirmed findings in this document, recheck changed sections with affected reviewers, and repeat until no confirmed unresolved findings remain. A factual external limitation remains a labeled blocked gate, never hidden as a completed test.

Implementation report must include commit/local branch, exact changed paths, full offline command results, runtime line count/import list, live tuple or precise missing inputs, accepted session/PR evidence if any, CI status on exact published SHA if applicable, and explicit monitor-migration state. Never label planning review as implementation or live verification.

---

## Official References Checked 8 October 2026

- [Authentication](https://jules.google/docs/api/reference/authentication/)
- [Sources and branch discovery](https://jules.google/docs/api/reference/sources/)
- [Source pagination](https://developers.google.com/jules/api/reference/rest/v1alpha/sources/list)
- [Session create](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/create)
- [Session fields and states](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions)
- [Session pagination](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions/list)
- [Activity pagination](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions.activities/list)
- [Activity and artifact fields](https://developers.google.com/jules/api/reference/rest/v1alpha/sessions.activities)
- [GitHub latest check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
- [GitHub current commit statuses](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference)
- [Git apply](https://git-scm.com/docs/git-apply)
- [Git clone](https://git-scm.com/docs/git-clone)
- [Git command/configuration behavior](https://git-scm.com/docs/git)
