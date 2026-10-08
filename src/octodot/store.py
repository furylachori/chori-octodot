"""Durable state storage and recovery fences for octodot.

Standard library only. Compatible with Python 3.10+.
Short transactions, owner-only permissions, exclusive workflow locking,
and host-controlled profile configuration recovery fences.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Generator, Mapping, Protocol

from octodot.contracts import RecoveryFence, Store
from octodot.errors import ErrorCode, StateStoreError
from octodot.migrations import CURRENT_SCHEMA_VERSION, get_schema_version, migrate_database
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ArtifactManifest,
    Binding,
    CandidateBundle,
    Coverage,
    Event,
    Observation,
    OperationRecord,
    OperationState,
    Receipt,
    VerifiedGrant,
    is_legal_operation_transition,
)

ProfileEpochSource = RecoveryFence


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# Recovery Fences
# -----------------------------------------------------------------------------


class InMemoryRecoveryFence:
    """In-memory recovery fence for testing."""

    def __init__(
        self,
        epochs: dict[str, int] | None = None,
        checkpoints: dict[str, int] | None = None,
    ) -> None:
        self._epochs: dict[str, int] = dict(epochs or {})
        self._checkpoints: dict[str, int] = dict(checkpoints or {})

    def get_current_epoch(self, profile: str) -> int:
        return self._epochs.get(profile, 0)

    def is_fence_valid(self, profile: str, recorded_epoch: int) -> bool:
        return recorded_epoch > 0 and recorded_epoch == self.get_current_epoch(profile)

    def get_journal_checkpoint(self, profile: str) -> int:
        return self._checkpoints.get(profile, 0)

    def advance_journal_checkpoint(self, profile: str, seq: int) -> None:
        current = self.get_journal_checkpoint(profile)
        if seq > current:
            self._checkpoints[profile] = seq

    def set_epoch(self, profile: str, epoch: int) -> None:
        self._epochs[profile] = epoch

    def advance_epoch(self, profile: str) -> int:
        new_epoch = self.get_current_epoch(profile) + 1
        self._epochs[profile] = new_epoch
        return new_epoch


class FileRecoveryFence:
    """Host-controlled recovery fence backed by an external file or directory outside the state dir.

    Stores ONLY the non-secret epoch integer and monotonic journal checkpoint sequence per profile,
    never any credentials or fingerprints.
    """

    def __init__(self, fence_path: str | Path) -> None:
        self._path = Path(fence_path).resolve()
        if self._path.is_dir() or str(self._path).endswith(os.sep):
            self._is_dir = True
            self._path.mkdir(parents=True, exist_ok=True)
        else:
            self._is_dir = False
            self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _atomic_write(self, target_path: Path, text: str) -> None:
        tmp_path = target_path.parent / f"{target_path.name}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(str(tmp_path), 0o600)
        os.replace(str(tmp_path), str(target_path))

    def _read_data(self) -> dict[str, dict[str, int]]:
        if self._is_dir:
            data: dict[str, dict[str, int]] = {}
            for item in self._path.glob("*.epoch"):
                profile = item.stem
                epoch = 0
                checkpoint = 0
                try:
                    c = item.read_text(encoding="utf-8").strip()
                    if c:
                        epoch = int(c)
                except (OSError, ValueError):
                    pass
                cp_file = self._path / f"{profile}.checkpoint"
                if cp_file.exists():
                    try:
                        c = cp_file.read_text(encoding="utf-8").strip()
                        if c:
                            checkpoint = int(c)
                    except (OSError, ValueError):
                        pass
                data[profile] = {"epoch": epoch, "checkpoint": checkpoint}
            return data
        else:
            if not self._path.exists():
                return {}
            try:
                content = self._path.read_text(encoding="utf-8").strip()
                if not content:
                    return {}
                raw = json.loads(content)
                result: dict[str, dict[str, int]] = {}
                for k, v in raw.items():
                    if isinstance(v, dict):
                        result[str(k)] = {
                            "epoch": int(v.get("epoch", 0)),
                            "checkpoint": int(v.get("checkpoint", v.get("journal_checkpoint", 0))),
                        }
                    elif isinstance(v, (int, str)):
                        result[str(k)] = {"epoch": int(v), "checkpoint": 0}
                return result
            except (OSError, ValueError, json.JSONDecodeError):
                return {}

    def get_current_epoch(self, profile: str) -> int:
        data = self._read_data()
        return data.get(profile, {}).get("epoch", 0)

    def is_fence_valid(self, profile: str, recorded_epoch: int) -> bool:
        return recorded_epoch > 0 and recorded_epoch == self.get_current_epoch(profile)

    def get_journal_checkpoint(self, profile: str) -> int:
        data = self._read_data()
        return data.get(profile, {}).get("checkpoint", 0)

    def set_epoch(self, profile: str, epoch: int) -> None:
        if self._is_dir:
            file_path = self._path / f"{profile}.epoch"
            self._atomic_write(file_path, f"{epoch}\n")
        else:
            data = self._read_data()
            prof_data = data.setdefault(profile, {"epoch": 0, "checkpoint": 0})
            prof_data["epoch"] = epoch
            self._atomic_write(self._path, json.dumps(data, indent=2))

    def advance_epoch(self, profile: str) -> int:
        current = self.get_current_epoch(profile)
        new_epoch = current + 1
        self.set_epoch(profile, new_epoch)
        return new_epoch

    def advance_journal_checkpoint(self, profile: str, seq: int) -> None:
        current = self.get_journal_checkpoint(profile)
        if seq <= current:
            return
        if self._is_dir:
            file_path = self._path / f"{profile}.checkpoint"
            self._atomic_write(file_path, f"{seq}\n")
        else:
            data = self._read_data()
            prof_data = data.setdefault(profile, {"epoch": 0, "checkpoint": 0})
            prof_data["checkpoint"] = seq
            self._atomic_write(self._path, json.dumps(data, indent=2))


# -----------------------------------------------------------------------------
# SQLite Store
# -----------------------------------------------------------------------------


class SQLiteStore:
    """Concrete SQLite state store implementing the Store protocol.

    Owns short transactions, owner-only state files, exclusive workflow lock,
    durable operations, events, receipts, scans, checkpoints, and recovery metadata.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        db_name: str = "octodot.db",
        lock_name: str = "workflow.lock",
        auto_migrate: bool = True,
        fault_hook: Callable[[str], None] | None = None,
        fence: RecoveryFence | None = None,
    ) -> None:
        str_raw = str(state_dir)
        if os.path.islink(str_raw):
            raise StateStoreError(
                ErrorCode.UNSAFE_STATE_DIR,
                f"State directory cannot be a symbolic link: {str_raw}",
            )
        self._state_dir = Path(state_dir).resolve()
        self._validate_and_prepare_state_dir(self._state_dir)

        self._db_path = self._state_dir / db_name
        self._lock_path = self._state_dir / lock_name
        self._fault_hook = fault_hook
        self._fence = fence
        self._lock_fd: int | None = None

        self._init_db(auto_migrate=auto_migrate)

        if self._fence is not None:
            self.recover_fence_checkpoints(self._fence)

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def set_fault_hook(self, hook: Callable[[str], None] | None) -> None:
        """Inject or clear a deterministic crash/fault hook."""
        self._fault_hook = hook

    # --- Directory and file permissions ---

    @staticmethod
    def _validate_and_prepare_state_dir(state_dir: Path) -> None:
        str_path = str(state_dir)
        if os.path.islink(str_path):
            raise StateStoreError(
                ErrorCode.UNSAFE_STATE_DIR,
                f"State directory cannot be a symbolic link: {str_path}",
            )

        if not state_dir.exists():
            try:
                state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.chmod(str_path, 0o700)
            except OSError as e:
                raise StateStoreError(
                    ErrorCode.UNSAFE_STATE_DIR,
                    f"Failed to create state directory: {e}",
                ) from e
        else:
            st = os.stat(str_path)
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise StateStoreError(
                    ErrorCode.UNSAFE_STATE_DIR,
                    f"State directory not owned by current user (UID {st.st_uid} != {os.getuid()})",
                )
            if bool(st.st_mode & 0o002):
                raise StateStoreError(
                    ErrorCode.UNSAFE_STATE_DIR,
                    f"State directory is world-writable: {str_path}",
                )
            if bool(st.st_mode & 0o020):
                raise StateStoreError(
                    ErrorCode.UNSAFE_STATE_DIR,
                    f"State directory is group-writable: {str_path}",
                )
            if bool(st.st_mode & 0o077):
                raise StateStoreError(
                    ErrorCode.UNSAFE_STATE_DIR,
                    f"State directory must have owner-only permissions (0700), found {oct(st.st_mode & 0o777)}",
                )

    def _ensure_file_permissions(self, file_path: Path) -> None:
        if file_path.exists():
            try:
                os.chmod(str(file_path), 0o600)
            except OSError:
                pass

    # --- Database Initialization ---

    def _init_db(self, auto_migrate: bool) -> None:
        db_existed = self._db_path.exists()
        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() or "busy" in str(e).lower():
                raise StateStoreError(ErrorCode.STATE_LOCKED, f"Database is locked: {e}") from e
            raise StateStoreError(ErrorCode.INTERNAL_ERROR, f"Cannot open database: {e}") from e
        except sqlite3.DatabaseError as e:
            raise StateStoreError(ErrorCode.STATE_CORRUPT, f"Database corrupt: {e}") from e

        self._ensure_file_permissions(self._db_path)

        try:
            # Integrity check
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            rows = cursor.fetchall()
            if not rows or rows[0][0] != "ok":
                raise StateStoreError(
                    ErrorCode.STATE_CORRUPT,
                    f"Database failed integrity check: {rows}",
                )
            # Enable WAL mode if possible
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA foreign_keys = ON")

            # Check existing schema version
            current_version = get_schema_version(self._conn)
            if current_version > CURRENT_SCHEMA_VERSION:
                raise StateStoreError(
                    ErrorCode.SCHEMA_TOO_NEW,
                    f"Database schema version {current_version} is newer than supported version {CURRENT_SCHEMA_VERSION}",
                )

            if auto_migrate:
                migrate_database(self._conn, fault_hook=self._fault_hook)
        except Exception as e:
            try:
                self._conn.close()
            except Exception:
                pass
            if isinstance(e, sqlite3.DatabaseError):
                raise StateStoreError(
                    ErrorCode.STATE_CORRUPT,
                    f"Database corruption detected: {e}",
                ) from e
            raise

        self._ensure_file_permissions(self._db_path)

    # --- Workflow Lock ---

    def acquire_lock(self, timeout: float = 0.0) -> bool:
        """Acquire exclusive process/workflow owner lock via fcntl."""
        if self._lock_fd is not None:
            return True

        try:
            fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            raise StateStoreError(ErrorCode.STATE_LOCKED, f"Cannot open lock file: {e}") from e

        self._ensure_file_permissions(self._lock_path)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                return True
            except (BlockingIOError, OSError) as e:
                err = getattr(e, "errno", None)
                if err in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
                    os.close(fd)
                    raise StateStoreError(
                        ErrorCode.LOCK_UNSUPPORTED,
                        f"File locking is unsupported: {e}",
                    ) from e
                if err not in (errno.EAGAIN, errno.EACCES, errno.EBUSY) and not isinstance(
                    e, BlockingIOError
                ):
                    os.close(fd)
                    raise StateStoreError(ErrorCode.STATE_LOCKED, f"Failed locking: {e}") from e
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.01)

    def release_lock(self) -> None:
        """Release workflow owner lock."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    # --- Short Transactions ---

    @contextmanager
    def transaction(
        self,
        fault_point_before: str | None = None,
        fault_point_after: str | None = None,
    ) -> Generator[sqlite3.Cursor, None, None]:
        """Short-lived transaction context manager. Never held across callbacks or sleep."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                raise StateStoreError(ErrorCode.STATE_LOCKED, f"Database is locked: {e}") from e
            if "disk full" in msg or "database or disk is full" in msg:
                raise StateStoreError(ErrorCode.INTERNAL_ERROR, f"Database or disk is full: {e}") from e
            raise StateStoreError(ErrorCode.INTERNAL_ERROR, f"Operational error: {e}") from e
        except sqlite3.DatabaseError as e:
            raise StateStoreError(ErrorCode.STATE_CORRUPT, f"Database corrupt: {e}") from e

        cursor = self._conn.cursor()
        try:
            yield cursor
            if self._fault_hook and fault_point_before:
                self._fault_hook(fault_point_before)
            self._conn.execute("COMMIT")
            if self._fault_hook and fault_point_after:
                self._fault_hook(fault_point_after)
        except Exception as e:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            if isinstance(e, sqlite3.OperationalError):
                msg = str(e).lower()
                if "disk full" in msg or "database or disk is full" in msg:
                    raise StateStoreError(ErrorCode.INTERNAL_ERROR, f"Database or disk is full: {e}") from e
                if "locked" in msg or "busy" in msg:
                    raise StateStoreError(ErrorCode.STATE_LOCKED, f"Database is locked: {e}") from e
            elif isinstance(e, sqlite3.DatabaseError):
                raise StateStoreError(ErrorCode.STATE_CORRUPT, f"Database corruption: {e}") from e
            raise

    # --- Profiles and Recovery Fence Reconciliation ---

    def set_fence(self, fence: RecoveryFence | None) -> None:
        """Set or update the trusted recovery fence and recover any ahead checkpoints."""
        self._fence = fence
        if fence is not None:
            self.recover_fence_checkpoints(fence)

    def recover_fence_checkpoints(self, fence: RecoveryFence | None = None) -> None:
        """Recover fence checkpoints on open/recovery if DB is ahead due to crash after commit.

        Crash between DB commit and fence advance leaves DB journal_seq > fence checkpoint.
        This re-advances the fence to the DB value only when the DB is ahead and epoch matches.
        It never lowers the fence (which would mask a stale restore).
        """
        target_fence = fence or self._fence
        if target_fence is None:
            return
        cursor = self._conn.cursor()
        cursor.execute("SELECT profile, reconciled_epoch, journal_seq FROM profiles")
        for row in cursor.fetchall():
            prof = row["profile"]
            db_epoch = int(row["reconciled_epoch"])
            db_seq = int(row["journal_seq"])
            host_epoch = target_fence.get_current_epoch(prof)
            fence_seq = target_fence.get_journal_checkpoint(prof)
            if db_epoch == host_epoch and db_seq > fence_seq:
                target_fence.advance_journal_checkpoint(prof, db_seq)

    def get_profile_epoch(self, profile: str) -> int:
        """Get host-recorded epoch for profile from the profiles table."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT reconciled_epoch FROM profiles WHERE profile = ?", (profile,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def get_profile_journal_seq(self, profile: str) -> int:
        """Get durable journal sequence for profile from the profiles table."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT journal_seq FROM profiles WHERE profile = ?", (profile,))
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _bump_journal_seq(self, profile: str) -> int:
        """Increment durable journal sequence for profile within current transaction."""
        now = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO profiles (
                profile, reconciled_epoch, journal_seq, identity_validated, created_at, updated_at
            ) VALUES (?, 0, 1, 0, ?, ?)
            ON CONFLICT(profile) DO UPDATE SET
                journal_seq = profiles.journal_seq + 1,
                updated_at = excluded.updated_at
            """,
            (profile, now, now),
        )
        return self.get_profile_journal_seq(profile)

    def is_profile_identity_validated(self, profile: str) -> bool:
        """Return True if fresh identity validation was recorded for profile."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT identity_validated FROM profiles WHERE profile = ?", (profile,))
        row = cursor.fetchone()
        return bool(row and row[0] == 1)

    def reconcile_profile_epoch(
        self,
        profile: str,
        epoch: int,
        identity_validated: bool = True,
        fence: RecoveryFence | None = None,
    ) -> None:
        """Record reconciliation of profile with host configuration epoch and identity validation."""
        target_fence = fence or self._fence
        now = _utc_now_iso()
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO profiles (
                    profile, reconciled_epoch, journal_seq, identity_validated, validated_at, created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    reconciled_epoch = excluded.reconciled_epoch,
                    identity_validated = excluded.identity_validated,
                    validated_at = excluded.validated_at,
                    updated_at = excluded.updated_at
                """,
                (profile, epoch, 1 if identity_validated else 0, now, now, now),
            )
        if target_fence is not None:
            target_fence.advance_journal_checkpoint(profile, self.get_profile_journal_seq(profile))

    def is_mutation_resumption_allowed(
        self, profile: str, fence: RecoveryFence | None = None
    ) -> bool:
        """Check if mutation resumption is allowed for profile under the given recovery fence.

        Requires:
        - Trusted recovery fence provided
        - Host configuration epoch > 0
        - DB reconciled epoch == host configuration epoch
        - Identity validated for current epoch
        - DB durable journal sequence == fence journal checkpoint
        """
        target_fence = fence or self._fence
        if target_fence is None:
            return False
        host_epoch = target_fence.get_current_epoch(profile)
        if host_epoch <= 0:
            return False
        db_epoch = self.get_profile_epoch(profile)
        if db_epoch != host_epoch:
            return False
        if not self.is_profile_identity_validated(profile):
            return False

        fence_seq = target_fence.get_journal_checkpoint(profile)
        db_seq = self.get_profile_journal_seq(profile)
        if db_seq < fence_seq:
            # Stale DB backup restore detected
            return False
        if db_seq > fence_seq:
            # DB ahead without recovery yet performed
            return False
        return True

    def check_mutation_eligibility(
        self, profile: str, fence: RecoveryFence | None = None
    ) -> None:
        """Raise StateStoreError(RECOVERY_FENCE_STALE) if mutation resumption is not allowed."""
        if not self.is_mutation_resumption_allowed(profile, fence):
            raise StateStoreError(
                ErrorCode.RECOVERY_FENCE_STALE,
                f"Recovery fence mismatch or unvalidated identity for profile '{profile}'; writes blocked",
            )

    # --- Operations ---

    def save_operation(
        self, record: OperationRecord, fence: RecoveryFence | None = None
    ) -> None:
        """Persist a new operation record (insert-only).

        Enforces single-attempt journal semantics and conflict detection:
        - If operation does not exist: inserts the operation and bumps the journal sequence.
        - If operation already exists with identical request_hash: returns without modifying state.
        - If operation already exists with different request_hash: raises StateStoreError(OPERATION_CONFLICT).
        """
        target_fence = fence or self._fence
        profile = record.binding.profile if record.binding else "default"
        now = _utc_now_iso()
        created_at = record.created_at or now
        updated_at = record.updated_at or now
        b = record.binding
        new_seq: int | None = None

        with self.transaction():
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT request_hash, state FROM operations WHERE operation_id = ?",
                (record.operation_id,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                existing_hash = existing["request_hash"]
                if existing_hash != record.request_hash:
                    raise StateStoreError(
                        ErrorCode.OPERATION_CONFLICT,
                        f"Operation '{record.operation_id}' already exists with different request hash "
                        f"'{existing_hash}' != '{record.request_hash}'",
                    )
                # Same request_hash: return/no-op without changing state
                return

            self._conn.execute(
                """
                INSERT INTO operations (
                    operation_id, state, request_hash, profile, profile_epoch, source,
                    repository, starting_branch, session, ticket_id, api_accepted,
                    effect_observed, attribution, ui_verified, accepted_identity_unverified,
                    error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.operation_id,
                    record.state.value if hasattr(record.state, "value") else str(record.state),
                    record.request_hash,
                    b.profile if b else None,
                    b.profile_epoch if b else None,
                    b.source if b else None,
                    b.repository if b else None,
                    b.starting_branch if b else None,
                    b.session if b else None,
                    record.ticket_id,
                    1 if record.api_accepted else 0,
                    1 if record.effect_observed else 0,
                    record.attribution,
                    1 if record.ui_verified else 0,
                    1 if record.accepted_identity_unverified else 0,
                    record.error_code.value
                    if record.error_code and hasattr(record.error_code, "value")
                    else (str(record.error_code) if record.error_code else None),
                    created_at,
                    updated_at,
                ),
            )
            for k, v in record.evidence:
                self._conn.execute(
                    """
                    INSERT INTO operation_evidence (operation_id, key, value_json, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (record.operation_id, k, json.dumps(v), now),
                )
            new_seq = self._bump_journal_seq(profile)

        if target_fence is not None and new_seq is not None:
            target_fence.advance_journal_checkpoint(profile, new_seq)

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        """Retrieve operation record by ID."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,))
        row = cursor.fetchone()
        if row is None:
            return None

        # Load evidence
        cursor.execute(
            "SELECT key, value_json FROM operation_evidence WHERE operation_id = ? ORDER BY evidence_id ASC",
            (operation_id,),
        )
        evidence_rows = cursor.fetchall()
        evidence_list: list[tuple[str, Any]] = []
        for erow in evidence_rows:
            try:
                val = json.loads(erow["value_json"])
            except Exception:
                val = erow["value_json"]
            evidence_list.append((erow["key"], val))

        binding: Binding | None = None
        if row["profile"] and row["source"] and row["repository"]:
            binding = Binding(
                profile=row["profile"],
                profile_epoch=row["profile_epoch"] or 0,
                source=row["source"],
                repository=row["repository"],
                starting_branch=row["starting_branch"],
                session=row["session"],
            )

        err_code: ErrorCode | None = None
        if row["error_code"]:
            try:
                err_code = ErrorCode(row["error_code"])
            except ValueError:
                err_code = None

        return OperationRecord(
            operation_id=row["operation_id"],
            state=OperationState(row["state"]),
            request_hash=row["request_hash"],
            binding=binding,
            ticket_id=row["ticket_id"],
            api_accepted=bool(row["api_accepted"]),
            effect_observed=bool(row["effect_observed"]),
            attribution=row["attribution"] or "",
            ui_verified=bool(row["ui_verified"]),
            accepted_identity_unverified=bool(row["accepted_identity_unverified"]),
            error_code=err_code,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            evidence=tuple(evidence_list),
        )

    def transition_operation_state(
        self,
        operation_id: str,
        to_state: OperationState,
        *,
        ticket_id: str | None = None,
        api_accepted: bool | None = None,
        effect_observed: bool | None = None,
        attribution: str | None = None,
        ui_verified: bool | None = None,
        accepted_identity_unverified: bool | None = None,
        error_code: ErrorCode | None = None,
        evidence_entry: tuple[str, Any] | None = None,
        fence: RecoveryFence | None = None,
    ) -> OperationRecord:
        """Compare-and-set state transition enforcing models' legal transition table."""
        target_fence = fence or self._fence
        new_seq: int | None = None
        profile = "default"

        with self.transaction():
            current = self.get_operation(operation_id)
            if current is None:
                raise StateStoreError(
                    ErrorCode.OPERATION_CONFLICT,
                    f"Operation '{operation_id}' does not exist for state transition",
                )

            if not is_legal_operation_transition(current.state, to_state):
                raise StateStoreError(
                    ErrorCode.OPERATION_CONFLICT,
                    f"Illegal operation transition from {current.state} to {to_state}",
                )

            profile = current.binding.profile if current.binding else "default"
            now = _utc_now_iso()

            # Monotonic evidence flags
            new_api_accepted = current.api_accepted or (api_accepted is True)
            new_effect_observed = current.effect_observed or (effect_observed is True)
            new_ui_verified = current.ui_verified or (ui_verified is True)
            new_accepted_identity_unverified = (
                current.accepted_identity_unverified or (accepted_identity_unverified is True)
            )
            new_attribution = attribution if attribution is not None else current.attribution

            self._conn.execute(
                """
                UPDATE operations SET
                    state = ?,
                    ticket_id = COALESCE(?, ticket_id),
                    api_accepted = ?,
                    effect_observed = ?,
                    attribution = ?,
                    ui_verified = ?,
                    accepted_identity_unverified = ?,
                    error_code = COALESCE(?, error_code),
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    to_state.value,
                    ticket_id,
                    1 if new_api_accepted else 0,
                    1 if new_effect_observed else 0,
                    new_attribution,
                    1 if new_ui_verified else 0,
                    1 if new_accepted_identity_unverified else 0,
                    error_code.value if error_code else None,
                    now,
                    operation_id,
                ),
            )
            if evidence_entry:
                k, v = evidence_entry
                self._conn.execute(
                    """
                    INSERT INTO operation_evidence (operation_id, key, value_json, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (operation_id, k, json.dumps(v), now),
                )
            new_seq = self._bump_journal_seq(profile)

        if target_fence is not None and new_seq is not None:
            target_fence.advance_journal_checkpoint(profile, new_seq)

        updated = self.get_operation(operation_id)
        assert updated is not None
        return updated

    def update_operation_evidence_flags(
        self,
        operation_id: str,
        *,
        api_accepted: bool | None = None,
        effect_observed: bool | None = None,
        attribution: str | None = None,
        ui_verified: bool | None = None,
        accepted_identity_unverified: bool | None = None,
        fence: RecoveryFence | None = None,
    ) -> OperationRecord:
        """Monotonically update operation evidence flags without state regression."""
        target_fence = fence or self._fence
        new_seq: int | None = None
        profile = "default"

        with self.transaction():
            current = self.get_operation(operation_id)
            if current is None:
                raise StateStoreError(
                    ErrorCode.OPERATION_CONFLICT,
                    f"Operation '{operation_id}' does not exist",
                )

            profile = current.binding.profile if current.binding else "default"
            now = _utc_now_iso()

            new_api_accepted = current.api_accepted or (api_accepted is True)
            new_effect_observed = current.effect_observed or (effect_observed is True)
            new_ui_verified = current.ui_verified or (ui_verified is True)
            new_accepted_identity_unverified = (
                current.accepted_identity_unverified or (accepted_identity_unverified is True)
            )
            new_attribution = attribution if attribution is not None else current.attribution

            self._conn.execute(
                """
                UPDATE operations SET
                    api_accepted = ?,
                    effect_observed = ?,
                    attribution = ?,
                    ui_verified = ?,
                    accepted_identity_unverified = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    1 if new_api_accepted else 0,
                    1 if new_effect_observed else 0,
                    new_attribution,
                    1 if new_ui_verified else 0,
                    1 if new_accepted_identity_unverified else 0,
                    now,
                    operation_id,
                ),
            )
            new_seq = self._bump_journal_seq(profile)

        if target_fence is not None and new_seq is not None:
            target_fence.advance_journal_checkpoint(profile, new_seq)

        updated = self.get_operation(operation_id)
        assert updated is not None
        return updated

    def append_operation_evidence(self, operation_id: str, key: str, value: Any) -> None:
        """Append an evidence record to an existing operation."""
        now = _utc_now_iso()
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO operation_evidence (operation_id, key, value_json, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (operation_id, key, json.dumps(value), now),
            )

    # --- Receipts ---

    def save_receipt(self, receipt: Receipt) -> None:
        """Persist a receiver/channel receipt (idempotent by receipt_id)."""
        meta_dict = dict(receipt.metadata)
        meta_json = json.dumps(meta_dict)
        now = receipt.timestamp or _utc_now_iso()

        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO receiver_receipts (
                    receipt_id, event_id, receiver_accepted, channel_send_accepted,
                    delivery_unknown, channel, timestamp, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO UPDATE SET
                    receiver_accepted = excluded.receiver_accepted,
                    channel_send_accepted = excluded.channel_send_accepted,
                    delivery_unknown = excluded.delivery_unknown,
                    channel = excluded.channel,
                    timestamp = excluded.timestamp,
                    metadata_json = excluded.metadata_json
                """,
                (
                    receipt.receipt_id,
                    receipt.event_id,
                    1 if receipt.receiver_accepted else 0,
                    1 if receipt.channel_send_accepted else 0,
                    1 if receipt.delivery_unknown else 0,
                    receipt.channel,
                    now,
                    meta_json,
                ),
            )

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        """Retrieve receipt by ID."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM receiver_receipts WHERE receipt_id = ?", (receipt_id,))
        row = cursor.fetchone()
        if row is None:
            return None

        meta_dict = json.loads(row["metadata_json"] or "{}")
        return Receipt(
            receipt_id=row["receipt_id"],
            event_id=row["event_id"],
            receiver_accepted=bool(row["receiver_accepted"]),
            channel_send_accepted=bool(row["channel_send_accepted"]),
            delivery_unknown=bool(row["delivery_unknown"]),
            channel=row["channel"],
            timestamp=row["timestamp"],
            metadata=tuple(meta_dict.items()),
        )

    # --- Events ---

    def save_event(self, event: Event) -> None:
        """Persist an event (idempotent by event_id)."""
        payload_dict = dict(event.payload)
        payload_json = json.dumps(payload_dict)
        now = event.created_at or _utc_now_iso()

        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO events (
                    event_id, event_type, resource_id, payload_json, created_at,
                    session_id, transition_id, acked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.resource_id,
                    payload_json,
                    now,
                    event.session_id,
                    event.transition_id,
                ),
            )

    def get_events(self, limit: int = 100, unacked_only: bool = False) -> tuple[Event, ...]:
        """Retrieve events in insertion order."""
        cursor = self._conn.cursor()
        if unacked_only:
            cursor.execute(
                "SELECT * FROM events WHERE acked = 0 ORDER BY rowid ASC LIMIT ?",
                (limit,),
            )
        else:
            cursor.execute("SELECT * FROM events ORDER BY rowid ASC LIMIT ?", (limit,))
        rows = cursor.fetchall()

        events: list[Event] = []
        for row in rows:
            p_dict = json.loads(row["payload_json"] or "{}")
            events.append(
                Event(
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    resource_id=row["resource_id"],
                    payload=tuple(p_dict.items()),
                    created_at=row["created_at"],
                    session_id=row["session_id"],
                    transition_id=row["transition_id"],
                )
            )
        return tuple(events)

    def ack_event(self, event_id: str, acked_at: str | None = None) -> bool:
        """Acknowledge an event by ID. Returns True if event was found and updated."""
        now = acked_at or _utc_now_iso()
        with self.transaction():
            cursor = self._conn.execute(
                "UPDATE events SET acked = 1, acked_at = ? WHERE event_id = ?",
                (now, event_id),
            )
            return cursor.rowcount > 0

    def is_event_acked(self, event_id: str) -> bool:
        """Check if an event is acknowledged."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT acked FROM events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        return bool(row and row["acked"] == 1)

    # --- Scans and Checkpoints ---

    def begin_scan(
        self,
        scan_id: str,
        profile: str,
        started_at: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Begin a new scan with complete=False."""
        now = started_at or _utc_now_iso()
        details_json = json.dumps(details or {})
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO scans (scan_id, profile, complete, started_at, details_json)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(scan_id) DO UPDATE SET
                    profile = excluded.profile,
                    complete = 0,
                    started_at = excluded.started_at,
                    details_json = excluded.details_json
                """,
                (scan_id, profile, now, details_json),
            )

    def commit_scan(
        self,
        scan_id: str,
        complete: bool,
        completed_at: str | None = None,
        coverage: Coverage | dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Commit a scan with complete status flag and coverage metadata."""
        now = completed_at or _utc_now_iso()
        cov_dict: dict[str, Any] = {}
        if isinstance(coverage, Coverage):
            cov_dict = {
                "complete": coverage.complete,
                "snapshot_atomic": coverage.snapshot_atomic,
                "pages": coverage.pages,
                "items": coverage.items,
                "skipped_scope": list(coverage.skipped_scope),
                "reasons": list(coverage.reasons),
                "resume_ref": coverage.resume_ref,
            }
        elif isinstance(coverage, Mapping):
            cov_dict = dict(coverage)
        cov_json = json.dumps(cov_dict)
        details_json = json.dumps(details) if details is not None else None

        with self.transaction(
            fault_point_before="before_scan_commit",
            fault_point_after="after_scan_commit",
        ):
            self._conn.execute(
                """
                UPDATE scans SET
                    complete = ?,
                    completed_at = ?,
                    coverage_json = ?,
                    details_json = COALESCE(?, details_json)
                WHERE scan_id = ?
                """,
                (1 if complete else 0, now, cov_json, details_json, scan_id),
            )

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        """Retrieve scan record by ID."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "scan_id": row["scan_id"],
            "profile": row["profile"],
            "complete": bool(row["complete"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "coverage": json.loads(row["coverage_json"] or "{}"),
            "details": json.loads(row["details_json"] or "{}"),
        }

    def is_scan_complete(self, scan_id: str) -> bool:
        """Return True if scan exists and was committed with complete=True."""
        scan = self.get_scan(scan_id)
        return bool(scan and scan.get("complete"))

    def can_establish_absence(self, scan_id: str) -> bool:
        """Return True only if scan is complete with complete coverage."""
        scan = self.get_scan(scan_id)
        if not scan or not scan.get("complete"):
            return False
        cov = scan.get("coverage", {})
        return bool(cov.get("complete", False))

    def is_scan_write_eligible(self, scan_id: str) -> bool:
        """Return True only if scan provides complete unambiguous context for writes."""
        return self.can_establish_absence(scan_id)

    def advance_checkpoint(
        self,
        checkpoint_id: str,
        profile: str,
        scan_id: str,
        position: str = "",
    ) -> None:
        """Advance checkpoint only if referenced scan is complete."""
        if not self.is_scan_complete(scan_id):
            raise StateStoreError(
                ErrorCode.PARTIAL_COVERAGE,
                f"Cannot advance checkpoint on incomplete scan '{scan_id}'",
            )

        now = _utc_now_iso()
        with self.transaction(
            fault_point_before="before_checkpoint_commit",
            fault_point_after="after_checkpoint_commit",
        ):
            self._conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_id, profile, scan_id, position, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id) DO UPDATE SET
                    profile = excluded.profile,
                    scan_id = excluded.scan_id,
                    position = excluded.position,
                    created_at = excluded.created_at
                """,
                (checkpoint_id, profile, scan_id, position, now),
            )

    def get_checkpoint(self, profile: str) -> dict[str, Any] | None:
        """Retrieve latest checkpoint for profile."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT * FROM checkpoints WHERE profile = ? ORDER BY rowid DESC LIMIT 1",
            (profile,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "checkpoint_id": row["checkpoint_id"],
            "profile": row["profile"],
            "scan_id": row["scan_id"],
            "position": row["position"],
            "created_at": row["created_at"],
        }

    def commit_scan_bundle(
        self,
        scan_id: str,
        profile: str,
        observation: Observation,
        events: Sequence[Event] = (),
        coverage: Coverage | dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
        fault_point: str | None = None,
    ) -> None:
        """Atomically persist scan, observation, events, commit scan, and advance checkpoint.

        All operations execute inside a single transaction (BEGIN IMMEDIATE ... COMMIT).
        Reuses existing SQL statements from begin_scan, save_observation, save_event,
        commit_scan, and advance_checkpoint. No HTTP or callbacks inside it.
        Advances checkpoint only if coverage is complete.
        Supports fault injection at designated points.
        """
        now = _utc_now_iso()

        # 1. Prepare scan details and coverage dict
        scan_cov_dict: dict[str, Any] = {}
        is_complete = False
        target_coverage = coverage if coverage is not None else observation.coverage
        if isinstance(target_coverage, Coverage):
            is_complete = target_coverage.complete
            scan_cov_dict = {
                "complete": target_coverage.complete,
                "snapshot_atomic": target_coverage.snapshot_atomic,
                "pages": target_coverage.pages,
                "items": target_coverage.items,
                "skipped_scope": list(target_coverage.skipped_scope),
                "reasons": list(target_coverage.reasons),
                "resume_ref": target_coverage.resume_ref,
            }
        elif isinstance(target_coverage, Mapping):
            is_complete = bool(target_coverage.get("complete", False))
            scan_cov_dict = dict(target_coverage)

        # 2. Prepare observation fields
        obs_id = f"obs_{scan_id}"
        b_dict = (
            {
                "profile": observation.binding.profile,
                "profile_epoch": observation.binding.profile_epoch,
                "source": observation.binding.source,
                "repository": observation.binding.repository,
                "starting_branch": observation.binding.starting_branch,
                "session": observation.binding.session,
            }
            if observation.binding
            else None
        )
        cov_dict = (
            {
                "complete": observation.coverage.complete,
                "snapshot_atomic": observation.coverage.snapshot_atomic,
                "pages": observation.coverage.pages,
                "items": observation.coverage.items,
                "skipped_scope": list(observation.coverage.skipped_scope),
                "reasons": list(observation.coverage.reasons),
                "resume_ref": observation.coverage.resume_ref,
            }
            if observation.coverage
            else None
        )
        bundle_dict = (
            {
                "messages": list(observation.candidate_bundle.messages),
                "activities": list(observation.candidate_bundle.activities),
                "has_ambiguity": observation.candidate_bundle.has_ambiguity,
                "ambiguity_reasons": list(observation.candidate_bundle.ambiguity_reasons),
                "selected_activity_id": observation.candidate_bundle.selected_activity_id,
                "last_message_text": observation.candidate_bundle.last_message_text,
            }
            if observation.candidate_bundle
            else None
        )

        with self.transaction(
            fault_point_before=fault_point if fault_point != "after_observation_insert" else None
        ):
            # A. Begin scan
            self._conn.execute(
                """
                INSERT INTO scans (scan_id, profile, complete, started_at, details_json)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(scan_id) DO UPDATE SET
                    profile = excluded.profile,
                    complete = 0,
                    started_at = excluded.started_at,
                    details_json = excluded.details_json
                """,
                (scan_id, profile, now, json.dumps({})),
            )

            # B. Insert observation
            self._conn.execute(
                """
                INSERT INTO observations (
                    observation_id, scan_id, binding_json, sources_json, sessions_json,
                    activities_json, coverage_json, candidate_bundle_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    scan_id = excluded.scan_id,
                    binding_json = excluded.binding_json,
                    sources_json = excluded.sources_json,
                    sessions_json = excluded.sessions_json,
                    activities_json = excluded.activities_json,
                    coverage_json = excluded.coverage_json,
                    candidate_bundle_json = excluded.candidate_bundle_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at
                """,
                (
                    obs_id,
                    scan_id,
                    json.dumps(b_dict) if b_dict else None,
                    json.dumps(list(observation.sources)),
                    json.dumps(list(observation.sessions)),
                    json.dumps(list(observation.activities)),
                    json.dumps(cov_dict) if cov_dict else None,
                    json.dumps(bundle_dict) if bundle_dict else None,
                    json.dumps(dict(observation.metadata)),
                    now,
                ),
            )

            # Fault point: after observation insert and before commit
            if self._fault_hook and fault_point == "after_observation_insert":
                self._fault_hook("after_observation_insert")

            # C. Idempotent event inserts
            for event in events:
                payload_dict = dict(event.payload)
                payload_json = json.dumps(payload_dict)
                ev_created = event.created_at or now
                self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id, event_type, resource_id, payload_json, created_at,
                        session_id, transition_id, acked
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.resource_id,
                        payload_json,
                        ev_created,
                        event.session_id,
                        event.transition_id,
                    ),
                )

            # D. Commit scan
            self._conn.execute(
                """
                UPDATE scans SET
                    complete = ?,
                    completed_at = ?,
                    coverage_json = ?
                WHERE scan_id = ?
                """,
                (1 if is_complete else 0, now, json.dumps(scan_cov_dict), scan_id),
            )

            # E. Advance checkpoint only if complete
            if is_complete and checkpoint_id:
                self._conn.execute(
                    """
                    INSERT INTO checkpoints (checkpoint_id, profile, scan_id, position, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(checkpoint_id) DO UPDATE SET
                        profile = excluded.profile,
                        scan_id = excluded.scan_id,
                        position = excluded.position,
                        created_at = excluded.created_at
                    """,
                    (checkpoint_id, profile, scan_id, "", now),
                )

    # --- Jobs ---

    def create_job(
        self,
        job_id: str,
        profile: str,
        plan_id: str,
        status: str = "pending",
        details: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now_iso()
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, profile, plan_id, status, created_at, updated_at, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    profile = excluded.profile,
                    plan_id = excluded.plan_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    details_json = excluded.details_json
                """,
                (job_id, profile, plan_id, status, now, now, json.dumps(details or {})),
            )

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "job_id": row["job_id"],
            "profile": row["profile"],
            "plan_id": row["plan_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "details": json.loads(row["details_json"] or "{}"),
        }

    def update_job(
        self, job_id: str, status: str, details: dict[str, Any] | None = None
    ) -> None:
        now = _utc_now_iso()
        details_json = json.dumps(details) if details is not None else None
        with self.transaction():
            self._conn.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    updated_at = ?,
                    details_json = COALESCE(?, details_json)
                WHERE job_id = ?
                """,
                (status, now, details_json, job_id),
            )

    # --- Action Results ---

    def save_action_result(self, result: ActionResult, plan_id: str = "") -> None:
        cov_json: str | None = None
        if result.coverage:
            cov = result.coverage
            cov_json = json.dumps({
                "complete": cov.complete,
                "snapshot_atomic": cov.snapshot_atomic,
                "pages": cov.pages,
                "items": cov.items,
                "skipped_scope": list(cov.skipped_scope),
                "reasons": list(cov.reasons),
                "resume_ref": cov.resume_ref,
            })
        data_json = json.dumps(dict(result.data))
        now = _utc_now_iso()

        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO action_results (
                    action_id, plan_id, op, status, exit_code, error_code, coverage_json, data_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    op = excluded.op,
                    status = excluded.status,
                    exit_code = excluded.exit_code,
                    error_code = excluded.error_code,
                    coverage_json = excluded.coverage_json,
                    data_json = excluded.data_json,
                    created_at = excluded.created_at
                """,
                (
                    result.action_id,
                    plan_id,
                    result.op,
                    result.status.value if hasattr(result.status, "value") else str(result.status),
                    result.exit_code,
                    result.error_code.value
                    if result.error_code and hasattr(result.error_code, "value")
                    else (str(result.error_code) if result.error_code else None),
                    cov_json,
                    data_json,
                    now,
                ),
            )

    def get_action_result(self, action_id: str) -> ActionResult | None:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM action_results WHERE action_id = ?", (action_id,))
        row = cursor.fetchone()
        if row is None:
            return None

        cov: Coverage | None = None
        if row["coverage_json"]:
            cd = json.loads(row["coverage_json"])
            cov = Coverage(
                complete=cd["complete"],
                snapshot_atomic=cd.get("snapshot_atomic", False),
                pages=cd.get("pages", 0),
                items=cd.get("items", 0),
                skipped_scope=tuple(cd.get("skipped_scope", ())),
                reasons=tuple(cd.get("reasons", ())),
                resume_ref=cd.get("resume_ref"),
            )

        data_dict = json.loads(row["data_json"] or "{}")
        err_code: ErrorCode | str | None = None
        if row["error_code"]:
            try:
                err_code = ErrorCode(row["error_code"])
            except ValueError:
                err_code = row["error_code"]

        return ActionResult(
            action_id=row["action_id"],
            op=row["op"],
            status=ActionResultStatus(row["status"]),
            exit_code=int(row["exit_code"]),
            error_code=err_code,
            coverage=cov,
            data=tuple(data_dict.items()),
        )

    # --- Artifact Manifests ---

    def save_manifest(self, manifest: ArtifactManifest) -> None:
        now = manifest.created_at or _utc_now_iso()
        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO manifests (
                    artifact_id, path, content_hash, byte_count, media_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    path = excluded.path,
                    content_hash = excluded.content_hash,
                    byte_count = excluded.byte_count,
                    media_type = excluded.media_type,
                    created_at = excluded.created_at
                """,
                (
                    manifest.artifact_id,
                    manifest.path,
                    manifest.content_hash,
                    manifest.byte_count,
                    manifest.media_type,
                    now,
                ),
            )

    def get_manifest(self, artifact_id: str) -> ArtifactManifest | None:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM manifests WHERE artifact_id = ?", (artifact_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return ArtifactManifest(
            artifact_id=row["artifact_id"],
            path=row["path"],
            content_hash=row["content_hash"],
            byte_count=int(row["byte_count"]),
            media_type=row["media_type"],
            created_at=row["created_at"],
        )

    # --- Authorization Records ---

    def save_authorization_record(self, record: VerifiedGrant | dict[str, Any]) -> None:
        now = _utc_now_iso()
        if isinstance(record, VerifiedGrant):
            grant_id = f"{record.operation_id}:{record.profile}:{record.profile_epoch}"
            data = (
                grant_id,
                record.operation_id,
                record.profile,
                record.profile_epoch,
                record.source,
                record.repository,
                record.branch,
                record.payload_hash,
                record.context_hash,
                record.plan_hash,
                record.publication_scope,
                record.authorizing_source,
                record.session,
                record.expiry,
                record.revocation_ref,
                record.max_attempts,
                now,
            )
        else:
            data = (
                record["grant_id"],
                record["operation_id"],
                record["profile"],
                record["profile_epoch"],
                record["source"],
                record["repository"],
                record["branch"],
                record["payload_hash"],
                record["context_hash"],
                record["plan_hash"],
                record["publication_scope"],
                record["authorizing_source"],
                record.get("session"),
                record.get("expiry"),
                record.get("revocation_ref"),
                record.get("max_attempts", 1),
                now,
            )

        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO authorization_records (
                    grant_id, operation_id, profile, profile_epoch, source, repository,
                    branch, payload_hash, context_hash, plan_hash, publication_scope,
                    authorizing_source, session, expiry, revocation_ref, max_attempts, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(grant_id) DO UPDATE SET
                    operation_id = excluded.operation_id,
                    profile = excluded.profile,
                    profile_epoch = excluded.profile_epoch,
                    source = excluded.source,
                    repository = excluded.repository,
                    branch = excluded.branch,
                    payload_hash = excluded.payload_hash,
                    context_hash = excluded.context_hash,
                    plan_hash = excluded.plan_hash,
                    publication_scope = excluded.publication_scope,
                    authorizing_source = excluded.authorizing_source,
                    session = excluded.session,
                    expiry = excluded.expiry,
                    revocation_ref = excluded.revocation_ref,
                    max_attempts = excluded.max_attempts,
                    recorded_at = excluded.recorded_at
                """,
                data,
            )

    def get_authorization_record(self, grant_id: str) -> VerifiedGrant | None:
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM authorization_records WHERE grant_id = ?", (grant_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return VerifiedGrant(
            action="",
            operation_id=row["operation_id"],
            profile=row["profile"],
            profile_epoch=int(row["profile_epoch"]),
            source=row["source"],
            repository=row["repository"],
            branch=row["branch"],
            payload_hash=row["payload_hash"],
            context_hash=row["context_hash"],
            plan_hash=row["plan_hash"],
            publication_scope=row["publication_scope"],
            authorizing_source=row["authorizing_source"],
            session=row["session"],
            expiry=row["expiry"],
            revocation_ref=row["revocation_ref"],
            max_attempts=int(row["max_attempts"]),
        )

    # --- Observations ---

    def save_observation(
        self,
        observation: Observation,
        scan_id: str | None = None,
        observation_id: str | None = None,
    ) -> str:
        obs_id = observation_id or f"obs_{int(time.time() * 1000)}"
        now = _utc_now_iso()
        b_dict = (
            {
                "profile": observation.binding.profile,
                "profile_epoch": observation.binding.profile_epoch,
                "source": observation.binding.source,
                "repository": observation.binding.repository,
                "starting_branch": observation.binding.starting_branch,
                "session": observation.binding.session,
            }
            if observation.binding
            else None
        )
        cov_dict = (
            {
                "complete": observation.coverage.complete,
                "snapshot_atomic": observation.coverage.snapshot_atomic,
                "pages": observation.coverage.pages,
                "items": observation.coverage.items,
                "skipped_scope": list(observation.coverage.skipped_scope),
                "reasons": list(observation.coverage.reasons),
                "resume_ref": observation.coverage.resume_ref,
            }
            if observation.coverage
            else None
        )
        bundle_dict = (
            {
                "messages": list(observation.candidate_bundle.messages),
                "activities": list(observation.candidate_bundle.activities),
                "has_ambiguity": observation.candidate_bundle.has_ambiguity,
                "ambiguity_reasons": list(observation.candidate_bundle.ambiguity_reasons),
                "selected_activity_id": observation.candidate_bundle.selected_activity_id,
                "last_message_text": observation.candidate_bundle.last_message_text,
            }
            if observation.candidate_bundle
            else None
        )

        with self.transaction():
            self._conn.execute(
                """
                INSERT INTO observations (
                    observation_id, scan_id, binding_json, sources_json, sessions_json,
                    activities_json, coverage_json, candidate_bundle_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    scan_id = excluded.scan_id,
                    binding_json = excluded.binding_json,
                    sources_json = excluded.sources_json,
                    sessions_json = excluded.sessions_json,
                    activities_json = excluded.activities_json,
                    coverage_json = excluded.coverage_json,
                    candidate_bundle_json = excluded.candidate_bundle_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at
                """,
                (
                    obs_id,
                    scan_id,
                    json.dumps(b_dict) if b_dict else None,
                    json.dumps(list(observation.sources)),
                    json.dumps(list(observation.sessions)),
                    json.dumps(list(observation.activities)),
                    json.dumps(cov_dict) if cov_dict else None,
                    json.dumps(bundle_dict) if bundle_dict else None,
                    json.dumps(dict(observation.metadata)),
                    now,
                ),
            )
        return obs_id

    def get_observations(self, scan_id: str | None = None) -> tuple[Observation, ...]:
        cursor = self._conn.cursor()
        if scan_id is not None:
            cursor.execute(
                "SELECT * FROM observations WHERE scan_id = ? ORDER BY rowid ASC",
                (scan_id,),
            )
        else:
            cursor.execute("SELECT * FROM observations ORDER BY rowid ASC")
        rows = cursor.fetchall()

        results: list[Observation] = []
        for row in rows:
            b: Binding | None = None
            if row["binding_json"]:
                bd = json.loads(row["binding_json"])
                b = Binding(
                    profile=bd["profile"],
                    profile_epoch=bd["profile_epoch"],
                    source=bd["source"],
                    repository=bd["repository"],
                    starting_branch=bd.get("starting_branch"),
                    session=bd.get("session"),
                )

            cov: Coverage | None = None
            if row["coverage_json"]:
                cd = json.loads(row["coverage_json"])
                cov = Coverage(
                    complete=cd["complete"],
                    snapshot_atomic=cd.get("snapshot_atomic", False),
                    pages=cd.get("pages", 0),
                    items=cd.get("items", 0),
                    skipped_scope=tuple(cd.get("skipped_scope", ())),
                    reasons=tuple(cd.get("reasons", ())),
                    resume_ref=cd.get("resume_ref"),
                )

            bundle: CandidateBundle | None = None
            if row["candidate_bundle_json"]:
                bd = json.loads(row["candidate_bundle_json"])
                bundle = CandidateBundle(
                    messages=tuple(bd.get("messages", ())),
                    activities=tuple(bd.get("activities", ())),
                    has_ambiguity=bd.get("has_ambiguity", False),
                    ambiguity_reasons=tuple(bd.get("ambiguity_reasons", ())),
                    selected_activity_id=bd.get("selected_activity_id"),
                    last_message_text=bd.get("last_message_text"),
                )

            sources = tuple(json.loads(row["sources_json"] or "[]"))
            sessions = tuple(json.loads(row["sessions_json"] or "[]"))
            activities = tuple(json.loads(row["activities_json"] or "[]"))
            meta = tuple(json.loads(row["metadata_json"] or "{}").items())

            results.append(
                Observation(
                    binding=b,
                    sources=sources,
                    sessions=sessions,
                    activities=activities,
                    coverage=cov,
                    candidate_bundle=bundle,
                    metadata=meta,
                )
            )
        return tuple(results)

    # --- Close ---

    def close(self) -> None:
        """Release workflow lock and close database connection."""
        self.release_lock()
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
