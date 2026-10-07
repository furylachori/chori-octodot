# Baseline and Hygiene Record (S00)

**Date:** 2026-10-07  
**Slice:** S00 Freeze and sanitize baseline (re-scoped, greenfield)  
**Status:** Completed (Gate G0 waived by maintainer)

---

## 1. Maintainer Waiver of Plan Gate G0

On 2026-10-07, the project maintainer issued an explicit decision waiving plan Gate G0:
- **No legacy relay snapshot exists:** The project is a greenfield implementation.
- **No legacy files or behaviors:** `skills/relay-jules/scripts/jules_relay.py` and `tests/test_relay.py` are not created, and no synthetic legacy behaviors or fallback contracts are invented.
- **Legacy compatibility is not claimed:** This repository implements a clean-room portable Jules controller under `src/octodot/` without backward-compatibility obligations to earlier prototypes.

---

## 2. List of Not Applicable (N/A) Test Cases and Clauses

Due to the greenfield determination, the following test cases and plan clauses from [docs/IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) are categorized as Not Applicable (N/A):

1. **S00-T01** (*Original baseline test reproduction*): N/A — No legacy relay snapshot exists to run in a private workspace.
2. **S00-T02** (*Sanitized public mirror test suite*): N/A — No legacy test suite or code exists to import or sanitize.
3. **S00-T03** (*Before/after CLI output and fixture diff*): N/A — No legacy CLI entry point or historical fixtures exist.
4. **S03-T01 legacy migration part** (*Migrate empty and populated legacy DBs*): N/A — No legacy SQLite databases exist to migrate. The durable store implementation initializes and manages only its own versioned schema.
5. **S04-T02 legacy-facade clause** (*Legacy facade retains its documented explicit override*): N/A — No legacy facade or legacy CLI flags are implemented.
6. **S14 compat facade + legacy golden tests S14-T02 and baseline part of S14-T01**:
   - **S14 compat facade**: N/A — `skills/relay-jules/scripts/jules_relay.py` is not created as a compatibility facade.
   - **S14-T02** (*Legacy golden fixtures and JSONL/exit behavior*): N/A — No legacy golden fixtures or legacy CLI compatibility behaviors are provided.
   - **S14-T01 baseline part** (*Original/public 28-test baseline execution*): N/A — Only the complete new offline test suites are executed for S14 release verification.

---

## 3. Public Repository Hygiene Scan (S00-T04)

### Objective
Ensure that every git-tracked file contains zero private account identifiers, machine-specific filesystem paths, secrets or token patterns, SQLite database files, or real-looking session identifiers.

### Scan Method & Command
The hygiene scan inspects all git-tracked files (`git ls-files`) plus candidate stage files using Python standard library:

```sh
python3 -c "
import subprocess, re, sys, os

files = subprocess.check_output(['git', 'ls-files'], text=True).splitlines()
for extra in ['.gitignore', 'tests/__init__.py', 'docs/BASELINE.md']:
    if os.path.exists(extra) and extra not in files:
        files.append(extra)

patterns = {
    'email': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    'machine_path': re.compile(r'/[U]sers/|/[h]ome/'),
    'secret_token': re.compile(r'\b(?:gh[o]_[A-Za-z0-9]+|gh[p]_[A-Za-z0-9]+|AI[z]a[0-9A-Za-z_-]+|\bs[k]-[A-Za-z0-9_-]{20,}|BEGIN [P]RIVATE KEY)\b'),
    'session_id': re.compile(r'sessions/\d+'),
}

findings = []
for f in files:
    try:
        with open(f, 'rb') as fp:
            data = fp.read()
    except Exception as e:
        findings.append(f'Error reading {f}: {e}')
        continue

    if data.startswith(b'SQLite format 3') or f.endswith(('.sqlite', '.sqlite3', '.db')):
        findings.append(f'SQLite file detected: {f}')

    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        continue

    for name, pat in patterns.items():
        for i, line in enumerate(text.splitlines(), 1):
            for match in pat.finditer(line):
                findings.append(f'{f}:{i} [{name}] matched: {match.group(0)!r}')

if findings:
    print('HYGIENE FINDINGS DETECTED:')
    for finding in findings:
        print(f'  - {finding}')
    sys.exit(1)
else:
    print(f'PASS: {len(files)} files scanned, 0 violations detected.')
"
```

Individual shell verification checks:
```sh
# 1. Email addresses
git grep -E '\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b' || true

# 2. Machine-specific paths
git grep -E '/[U]sers/|/[h]ome/' || true

# 3. Secret tokens and key headers
git grep -E '\b(gh[o]_[A-Za-z0-9]+|gh[p]_[A-Za-z0-9]+|AI[z]a[0-9A-Za-z_-]+|\bs[k]-[A-Za-z0-9_-]{20,}|BEGIN [P]RIVATE KEY)\b' || true

# 4. SQLite files
git ls-files | grep -E '\.(sqlite|sqlite3|db)$' || true

# 5. Real-looking session IDs (e.g. sessions/<digits>)
git grep -E 'sessions/[0-9]+' || true
```

### Scan Results
- **Tracked & slice files scanned:**
  - `README.md`
  - `docs/IMPLEMENTATION_PLAN.md`
  - `docs/BASELINE.md`
  - `plan/implementation-plan.json`
  - `plan/implementation-plan.schema.json`
  - `.gitignore`
  - `tests/__init__.py`
- **Email addresses:** 0 found.
- **Machine paths:** 0 found.
- **Secret tokens:** 0 found.
- **SQLite databases:** 0 found.
- **Session IDs:** 0 real session IDs found (documentation uses synthetic placeholders such as `sessions/EXAMPLE` or public Google documentation links).
- **Public documentation links:** Verified as official public documentation links (e.g., `developers.google.com`, `jules.google`, `docs.github.com`).
- **Result:** **PASS (0 violations)**.

---

## 4. Test Harness and State Exclusions

1. **`tests/__init__.py`**:
   - Bootstraps `sys.path` to include `<repo>/src` computed dynamically from `__file__`.
   - Idempotent and free of external dependencies or side effects.
2. **`.gitignore`**:
   - Excludes Python caches, compilation outputs, virtual environments, private secrets (`*.pem`, `*.key`), and runtime state (`*.sqlite*`, `*.db`, `*-journal`, `*-wal`, `*-shm`, `*.lock`, `state/`, `.octodot/`, `artifacts/`, `evidence/private/`).
   - Retains public evidence summaries via `!docs/evidence/*.summary.json`.
