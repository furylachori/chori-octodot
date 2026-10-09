# octodot

A single stateless, dependency-free Python script (`octodot.py`) for interacting with the Google Jules REST API.

`octodot` replaces the legacy multi-module controller with a single, standalone CLI tool built entirely on the Python 3.10+ standard library. It requires zero third-party packages, maintains no local daemon or database, and interacts directly with the Jules API over HTTPS. Git is invoked solely for optional local repository operations (`--apply` and `-teleport`).

---

## Documentation

- **[Operations Guide](docs/OPERATIONS.md)**: Full CLI grammar, option matrix, exit codes, output envelopes, error diagnostics, reconciliation, and local Git mutation contract.
- **[Codex Workflow Guide](docs/CODEX_WORKFLOW.md)**: One-shot delivery, question answering, plan approval, successor handoff, and coordinator replay rules.
- **[Release Checklist](docs/RELEASE_CHECKLIST.md)**: Section 11 offline verification gates and Section 12 live acceptance and activation procedures.
- **[Implementation Plan](docs/IMPLEMENTATION_PLAN.md)**: Full Issue #3 specification and completion evidence.

---

## Status

- **Offline Verification**: Fully tested and verified offline across Python 3.10, 3.11, 3.12, and 3.13 on Linux and macOS. The complete offline test suite in `test_octodot.py` executes without network access, using synthetic fixtures and deterministic fault injection.
- **Live Acceptance**: Live Jules session creation, PR publication, and monitor activation require an explicitly authorized execution tuple (Section 12) and have **not** been run as part of offline implementation.
- **Legacy Retirement**: The legacy controller (`src/octodot`), SQLite journal/state databases, daemon runner, and plan schemas are retired. Old `python -m octodot` entrypoints are deprecated and replaced by standalone `octodot.py`.

---

## Requirements & Architecture

- **Runtime**: Python 3.10 or later (standard library only; no external runtime dependencies).
- **Git**: Git >= 2.36 (required only for local repository operations: `-new` repo/branch inference, `pull --apply`, and `teleport`).
- **Platform**: Linux and macOS. Windows is unsupported for local Git mutations (exits with code 3).
- **Authentication**: `JULES_API_KEY` environment variable. No credentials are stored on disk or accepted via CLI flags.

---

## Quickstart

Ensure `JULES_API_KEY` is set in your environment:

```bash
export JULES_API_KEY="your-api-key-here"
```

### Basic Commands

```bash
# Check version and help
python3 octodot.py --version
python3 octodot.py --help

# List connected repository sources
python3 octodot.py -list-repos

# List all sessions
python3 octodot.py -list-sessions

# Check single session status
python3 octodot.py -status sessions/SESSION_ID

# List session activities
python3 octodot.py -activities sessions/SESSION_ID

# Fetch comprehensive session results and patch metadata
python3 octodot.py -results sessions/SESSION_ID

# Send a reply to an active session
python3 octodot.py -reply sessions/SESSION_ID -prompt "Focus only on tests"

# Approve a pending plan
python3 octodot.py -approve-plan sessions/SESSION_ID
```

---

## Creating Sessions (`-new`)

The `-new` action creates one or more alternative Jules sessions for a prompt.

```bash
# Explicit repository and branch
python3 octodot.py -new -prompt "Fix typo in README" --repo OWNER/REPO --branch main

# Inferred repository and branch from current Git working tree
python3 octodot.py -new -prompt "Add error handling to parser"

# Read prompt from stdin
cat prompt.txt | python3 octodot.py -new --repo OWNER/REPO --branch main

# Explicit stdin prompt with current directory inference
python3 octodot.py -new -prompt - --repo . < prompt.txt

# Bounded parallel alternatives (up to 5 concurrent in-flight)
python3 octodot.py -new -prompt "Implement feature X" --repo OWNER/REPO --branch main --parallel 3 --title "Feature X task"
```

### Prompt Resolution Rules

1. **Literal prompt (`-prompt "..."`)**: Must contain non-whitespace characters. Preserves exact Unicode and whitespace. A literal prompt takes precedence over non-TTY stdin and never reads from it.
2. **Explicit stdin (`-prompt -`)**: Reads UTF-8 standard input until EOF. Invocations with `-prompt -` on an interactive TTY exit with code 2.
3. **Piped/redirected stdin**: If `-prompt` is omitted and standard input is not a TTY, the prompt is read from standard input.
4. **Interactive TTY check**: Omitting `-prompt` when standard input is a TTY results in an immediate exit 2 error.

---

## Inspecting and Exporting Patches (`-pull`)

```bash
# Output raw unidiff patch bytes to stdout
python3 octodot.py -pull sessions/SESSION_ID > change.patch

# Output structured patch metadata and patch content as JSON
python3 octodot.py -pull sessions/SESSION_ID --json

# Select a specific activity artifact explicitly
python3 octodot.py -pull sessions/SESSION_ID --activity sessions/SESSION_ID/activities/ACTIVITY_ID --artifact 0 --json
```

---

## Local Git Mutations (`-pull --apply` & `-teleport`)

> [!WARNING]
> Local patch application modifies files directly in your working directory without creating a commit or stash. Ensure your worktree is backed up or tracked.

### Apply Patch to Current Repository

```bash
python3 octodot.py -pull sessions/SESSION_ID --apply --cwd /path/to/repo
```

**Preconditions for `--apply`:**
- Operating system must be Linux or macOS.
- Git version must be $\ge 2.36$.
- Target repository working tree must be strictly clean (including untracked and ignored files; `git status --porcelain=v1 -z --untracked-files=all --ignored=matching` must be empty).
- Current `HEAD` commit must match the patch's `baseCommitId` exactly.
- Working tree must have no submodules, symlinks, sparse checkouts, or skip-worktree flags.
- Patches containing renames, file copies, `.gitmodules` modifications, or unsafe paths are strictly rejected.
- Git `HEAD` and index bytes are verified before and after to ensure worktree-only mutation.

### Teleport Patch to a Fresh Clone

```bash
python3 octodot.py -teleport sessions/SESSION_ID --dir /path/to/new-directory --apply
```

Clones the repository from GitHub into a new directory, checks out a new local branch named `octodot/SESSION_SUFFIX` at the patch's `baseCommitId`, and applies the patch to the working tree. The destination directory must not already exist, and its parent directory must be writable.

---

## Replying to Sessions & Approving Plans (`-reply`, `-approve-plan`)

Interactive session mutations are strictly single-attempt:

```bash
# Reply with literal prompt
python3 octodot.py -reply sessions/SESSION_ID -prompt "Focus only on tests"

# Reply using stdin prompt
cat clarification.txt | python3 octodot.py -reply sessions/SESSION_ID -prompt -

# Approve a real pending plan
python3 octodot.py -approve-plan sessions/SESSION_ID
```

- **Single-Attempt Writes**: `reply` and `approve-plan` mutations are never automatically retried upon timeout, disconnect, or server error.
- **Truthful Outcome Status**: Success outputs `"outcome": "acknowledged"`. Unconfirmed writes output `"outcome": "unconfirmed"` with exit code 5. Interrupted writes output exit code 130 with `"outcome": "never_dispatched"` or `"outcome": "unconfirmed"`.
- **Zero Git Invocation**: `reply` and `approve-plan` execute solely over HTTPS and make zero local Git calls.

---

## Output Format & Exit Codes

### Structured JSON Envelopes

- **Non-new actions**: Output a single JSON object on stdout:
  ```json
  {
    "action": "status",
    "ok": true,
    "complete": true,
    "data": { ... },
    "error": null
  }
  ```
- **New action**: Streams JSON Lines (JSONL) on stdout containing one attempt record per requested ordinal, followed by a summary record:
  ```json
  {"type": "attempt", "attempt": 1, "outcome": "accepted", "name": "sessions/...", ...}
  {"type": "summary", "requested": 1, "accepted": 1, "ok": true, "exitCode": 0, ...}
  ```
- **Diagnostic output**: Diagnostic receipts, progress events, and sanitized error messages are flushed immediately to stderr.
- **Redaction**: All occurrences of the active `JULES_API_KEY` are recursively redacted from all outputs and error messages.

### Exit Codes & Precedence

When multiple failure conditions arise, exit codes are determined by the following strict precedence:

$$\mathbf{130} \text{ (SIGINT/SIGTERM)} > \mathbf{5} \text{ (Uncertain / Mismatch)} > \mathbf{3} \text{ (Auth / Configuration)} > \mathbf{4} \text{ (Transport / Protocol / Git)} > \mathbf{2} \text{ (Usage / CLI)} > \mathbf{0} \text{ (Success)}$$

| Code | Meaning |
|:---:|:---|
| **0** | Success (operation completed successfully). |
| **2** | CLI argument parsing or local validation error (before remote communication). |
| **3** | Authentication failure (HTTP 401/403), missing `JULES_API_KEY`, Git < 2.36, or unsupported OS. |
| **4** | Transport error, HTTP 5xx, protocol error, quota exhaustion, Git mutation failure, deadline exceeded. |
| **5** | Uncertain outcome (POST unconfirmed, timeout/disconnect during mutation), known-created unverified, or context mismatch. |
| **130** | Process interrupted by SIGINT or SIGTERM. |

---

## Operational Limitations

1. **Max Parallel In-Flight**: At most 5 concurrent POST requests are in flight at any given moment, even if `--parallel` is set up to 100.
2. **Single-Attempt Writes**: POST requests are never retried under any circumstances to prevent duplicate session creation.
3. **No Automatic Reconciliation**: Uncertain creations emit exit code 5 and require manual inspection or coordinating agent verification.
4. **Git Mutation Scope**: Rename and copy patches, symlinks, submodules, and `.gitmodules` modifications are not supported by the local apply engine.

---

## License

No license has been selected. A license file and license metadata are intentionally absent pending the project owner's choice.
