# octodot Release & Verification Checklist

This document details the complete verification checklist for `octodot.py`, dividing verification into:
1. **Section 11: Offline Verification Gates** (required for implementation completion and PR acceptance).
2. **Section 12: Live Acceptance & Activation Gates** (strictly gated on explicit runtime authorization inputs).

---

## Part 1: Section 11 Offline Verification Gates

All offline gates must execute without network connectivity, using synthetic mocks, local fixtures, and the Python standard library.

### 1. Source Tree & Allowlist Verification

- [ ] Working branch is verified as `impl/plain-octodot`.
- [ ] Tracked files match the exact allowlist enforced by `ArchitectureTests`:
  - `octodot.py`
  - `test_octodot.py`
  - `README.md`
  - `docs/IMPLEMENTATION_PLAN.md`
  - `docs/OPERATIONS.md`
  - `docs/RELEASE_CHECKLIST.md`
  - `.github/workflows/offline.yml`
  - `cloudbuild.yaml`
  - `cloudbuild-offline.yaml`
  - `docs/CI.md`
  - `docs/CODEX_WORKFLOW.md`
  - `.gitignore`
- [ ] Tracked legacy directories and files are removed (`src/`, `tests/`, `examples/`, `plan/`, `schemas/`, `pyproject.toml`, `requirements-dev.txt`, `docs/AUTHORIZATION.md`, `docs/BASELINE.md`, `docs/CONTRACTS.md`).
- [ ] No external dependencies added; no `requirements.txt` or `setup.py`.

---

### 2. Individual Component Test Suites

All 11 unit test classes in `test_octodot.py` must pass with 0 failures, 0 errors, and 0 skipped tests:

- [ ] **`ParserTests`**:
  - All 8 actions and their single-dash/double-dash aliases.
  - Action mutual exclusivity (rejecting multiple actions).
  - Illegal option combinations (e.g. `--json` with `--apply`).
  - Duplicate option detection across aliases.
  - Prompt precedence (literal prompt vs non-TTY stdin vs explicit stdin `-`).
  - TTY checks and empty prompt rejection.
  - Non-finite timeout and deadline validation.
  - Session and activity name normalization and injection defense.
  - Repository and branch syntax validation.
  - Parallel boundaries (rejecting 0 and 101, admitting 1–100).
- [ ] **`OutputTests`**:
  - Envelope schema validation for non-new JSON commands.
  - JSON Lines output format and ordinal sequencing for `-new`.
  - Exactly one summary line emitted for `-new`.
  - Whole-line atomic locking across concurrent workers.
  - Recursive key redaction across all output values and provider errors.
  - Provider message truncation to 2048 characters.
  - Strict exit code precedence ($130 > 5 > 3 > 4 > 2 > 0$).
  - Signal handling (`SIGINT` and `SIGTERM`) leading to exit 130.
- [ ] **`ArchitectureTests`**:
  - Verification of exact 8 tracked files.
  - Prohibited module import checks (`sqlite3`, third-party packages).
  - No `eval`, `exec`, or dynamic import usage in production runtime.
  - Verification of `octodot 1.0.0` version string.
  - Verifying `--help` and `--version` operate completely offline.
- [ ] **`TransportTests`**:
  - Base URL and endpoint construction.
  - Header formatting (`X-Goog-Api-Key`, `Accept`, `Content-Type`).
  - Disabling of all HTTP redirects (preventing replay and leak).
  - Bounded GET retries (at most 3 attempts for 408, 429, 5xx, and transport errors).
  - Exponential backoff (1s, 2s) and `Retry-After` header parsing.
  - Immediate non-retry for 401, 403, and TLS errors.
  - Admission deadline enforcement.
- [ ] **`PaginationTests`**:
  - Handling multi-page responses with opaque `pageToken`.
  - Empty middle pages with valid next page tokens.
  - Detection and rejection of token cycles and duplicate resource names.
  - Preservation of accumulated items when a subsequent page fails.
- [ ] **`SourceTests`**:
  - Handling returned slash-containing opaque source names.
  - Case-insensitive owner/repo matching.
  - Rejection of zero or multiple matching sources.
  - Default branch and explicit branch resolution.
  - Git remote origin inference (HTTPS, SSH, SCP) and rejection of invalid URLs.
  - Detached HEAD detection requiring explicit `--branch`.
- [ ] **`ReadTests`**:
  - Raw session and activity data preservation.
  - Session state classification mapping (`pending`, `blocked`, `failed`, `completed`, `unknown`).
  - Handling of `COMPLETED` sessions with missing PR (extra GET check).
  - Delivery evaluation (`pr_reported`, `completed_without_pr`).
- [ ] **`CreateTests`**:
  - Exact JSON request body construction (omitting title if absent).
  - SHA-256 canonical body fingerprinting.
  - Diagnostic event emissions (`create_started`, `create_accepted`).
  - Returned name vs ID handling.
  - Strict zero POST retries under all transport and server error conditions.
  - Context verification and detection of context mismatch.
- [ ] **`ParallelTests`**:
  - Strict ceiling of at most 5 concurrent requests in flight.
  - Draining all completed workers before refilling.
  - Shared stop event triggering on any lane failure or uncertainty.
  - Cancellation of unsubmitted lanes (marked as `not_started`).
  - Exact summary counter accounting.
- [ ] **`ArtifactTests`**:
  - Extraction and inventory of `gitPatch` candidates.
  - Full nanosecond timestamp comparison for latest candidate selection.
  - Detection of ambiguous patches on missing timestamps or ties.
  - Fresh activity GET before export to detect artifact modifications.
  - `secret_in_artifact` rejection when patch contains `JULES_API_KEY`.
- [ ] **`GitTests`**:
  - Git version check ($\ge 2.36$).
  - Clean worktree enforcement (`git status --porcelain=v1 -z`).
  - Exact matching of `HEAD` commit to `baseCommitId`.
  - Rejection of patches containing renames, file copies, `.gitmodules`, or mode changes.
  - Verification that index file bytes and HEAD commit remain identical post-apply.
  - Teleport destination validation (`os.path.lexists`), checkout of `octodot/SESSION`, and patch application.

---

### 3. Exact Execution Sequence

Run the complete offline suite in the prescribed order from the repository root:

```bash
# 1. Environment and version checks
python3 --version
git --version

# 2. Syntax and compilation checks
python3 -m py_compile octodot.py test_octodot.py

# 3. CLI help and version checks
python3 octodot.py --help
python3 octodot.py --version

# 4. Wave 1: Parsing, output, and architecture
python3 -m unittest -v test_octodot.ParserTests test_octodot.OutputTests test_octodot.ArchitectureTests

# 5. Wave 2: Transport, pagination, source, and reads
python3 -m unittest -v test_octodot.TransportTests test_octodot.PaginationTests test_octodot.SourceTests test_octodot.ReadTests

# 6. Wave 3: Creation and parallel dispatch
python3 -m unittest -v test_octodot.CreateTests test_octodot.ParallelTests

# 7. Wave 4: Artifacts and local Git mutations
python3 -m unittest -v test_octodot.ArtifactTests test_octodot.GitTests

# 8. Full aggregate run
python3 -m unittest -v test_octodot

# 9. Git cleanliness check
git diff --check
```

- [ ] All commands exit with code 0.
- [ ] `git diff --check` reports zero whitespace or merge marker issues.

---

### 4. CI Workflow Gate (`.github/workflows/offline.yml`)

- [ ] 8-job matrix defined:
  - OS: `[ubuntu-latest, macos-latest]`
  - Python: `["3.10", "3.11", "3.12", "3.13"]`
- [ ] Triggers: `push` and `pull_request` on `main` and `impl/**`.
- [ ] Permissions: `contents: read`.
- [ ] Zero secrets, zero `pip install`, zero external packages.
- [ ] Timeout set to 10 minutes; `fail-fast: false`.
- [ ] All 8 matrix jobs pass on the exact published commit SHA.

---

## Part 2: Section 12 Live Acceptance & Activation Gates

> [!IMPORTANT]
> Live acceptance gates require an explicit authorization tuple supplied by the coordinator or user. Offline development cannot and must not perform live creation.

### Required Authorization Inputs

Before executing live validation, confirm the presence of:
- [ ] **Execution Environment**: Authorized runner / Codex container.
- [ ] **Target Repository**: Verified connected GitHub repository (`OWNER/REPO`).
- [ ] **Starting Branch**: Existing target branch.
- [ ] **Task Instructions**: Exact bounded prompt text.
- [ ] **Expected PR Base**: Target branch for resulting PR.
- [ ] **Single-Session Permission**: Explicit approval for ONE Jules creation call.
- [ ] *(For corrections)*: Original PR URL, head repository/branch, reviewed head SHA, and confirmed findings list.

---

### Execution Steps

- [ ] **Step 1: Read-Only Preflight**:
  - Run `python3 octodot.py -list-repos`.
  - Confirm repository source exists and starting branch is recognized.
  - Confirm `JULES_API_KEY` is valid.
- [ ] **Step 2: Pre-POST Alignment Verification**:
  - For corrections, verify target repo matches original PR head repo.
  - Verify starting branch matches original PR head ref.
  - Verify original head SHA is an ancestor of the intended change.
- [ ] **Step 3: Single Session Dispatch**:
  - Execute `python3 octodot.py -new -prompt "..." --repo OWNER/REPO --branch BRANCH --parallel 1 --timeout 30 --deadline 120`.
  - Capture pre-POST diagnostic receipt and stdout attempt record.
  - Run `python3 octodot.py -status sessions/SESSION_ID` to verify session status.
- [ ] **Step 4: Observation Cadence**:
  - Poll read-only status every 60 seconds (or via 30-minute background monitor).
  - Halt polling upon reaching terminal states (`COMPLETED`, `FAILED`, `blocked`).
- [ ] **Step 5: PR Verification**:
  - Confirm resulting PR base matches expected base.
  - Review diff against task requirements.
  - Paginate GitHub check runs and commit statuses for latest head SHA.
- [ ] **Step 6: Topology Check**:
  - If PR repository, base, or ancestry does not match expectations, label `blocked_topology` and halt.
- [ ] **Step 7: Artifact Provenance Check**:
  - Run `python3 octodot.py -pull sessions/SESSION_ID --json` to verify artifact extraction and patch SHA-256.
- [ ] **Step 8: Verdict Recording**:
  - Record definitive verdict: `live_passed`, `live_failed`, `live_blocked`, `live_pending`, or `live_not_run`.
