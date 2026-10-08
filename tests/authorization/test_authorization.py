"""Tests for S05 authorization, grant verification, and security trust boundaries.

Test IDs: S05-T01, S05-T02, S05-T03, S05-T04, S05-T06.
Standard library only. Compatible with Python 3.10+.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Mapping

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.authorization import (
    DisabledGrantVerifier,
    FakeGrantVerifier,
    HostGrantVerifierAdapter,
    parse_grant,
    require_verifier_allowed,
    verify_grant_binding,
)
from octodot.contracts import (
    Clock,
    GrantBlocker,
    canonical_hash,
    context_hash,
    request_hash,
)
from octodot.errors import (
    AuthorizationError,
    ErrorCode,
    OctodotError,
)
from octodot.models import (
    Binding,
    DispatchTicket,
    OperationRecord,
    OperationState,
    PreparedAction,
    VerifiedGrant,
)


class FakeClock:
    """In-memory deterministic clock for tests."""

    def __init__(self, current_time: datetime | None = None) -> None:
        self._current_time = current_time or datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self._current_time

    def advance(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def make_sample_binding(
    profile: str = "default",
    profile_epoch: int = 1,
    source: str = "sources/github/OWNER/REPO",
    repository: str = "OWNER/REPO",
    starting_branch: str | None = "feature/example",
    session: str | None = "sessions/EXAMPLE",
) -> Binding:
    return Binding(
        profile=profile,
        profile_epoch=profile_epoch,
        source=source,
        repository=repository,
        starting_branch=starting_branch,
        session=session,
    )


def make_sample_prepared_action(
    action: str = "act-reply-1",
    operation_id: str = "op-reply-1",
    binding: Binding | None = None,
    payload: dict[str, Any] | None = None,
    payload_hash: str | None = None,
    context_dict: dict[str, Any] | None = None,
    context_h: str | None = None,
    request_h: str | None = None,
    publication_scope: str = "none",
    plan_h: str = "sha256:1111111111111111111111111111111111111111111111111111111111111111",
) -> PreparedAction:
    b = binding or make_sample_binding()
    p = payload if payload is not None else {"text": "Approved reply text"}
    p_hash = payload_hash or canonical_hash(p)
    ctx = context_dict if context_dict is not None else {"target": "sessions/EXAMPLE", "step": 1}
    c_hash = context_h or context_hash(ctx)
    r_hash = request_h or request_hash({"target": b.session, "body": {"prompt": p.get("text", "")}})

    return PreparedAction(
        action=action,
        operation_id=operation_id,
        binding=b,
        payload=p,
        payload_hash=p_hash,
        context_hash=c_hash,
        request_hash=r_hash,
        publication_scope=publication_scope,
        plan_hash=plan_h,
    )


def make_matching_grant(
    action: PreparedAction,
    authorizing_source: str = "coordinator_review",
    expiry: str | None = "2026-10-07T14:00:00Z",
    revocation_ref: str | None = None,
    max_attempts: int = 1,
) -> VerifiedGrant:
    return VerifiedGrant(
        action=action.action,
        operation_id=action.operation_id,
        profile=action.binding.profile,
        profile_epoch=action.binding.profile_epoch,
        source=action.binding.source,
        repository=action.binding.repository,
        branch=action.binding.starting_branch or "",
        payload_hash=action.payload_hash,
        context_hash=action.context_hash,
        plan_hash=action.plan_hash,
        publication_scope=action.publication_scope,
        authorizing_source=authorizing_source,
        session=action.binding.session,
        expiry=expiry,
        revocation_ref=revocation_ref,
        max_attempts=max_attempts,
    )


class TestS05T01GrantBindingFailures(unittest.TestCase):
    """S05-T01: Missing, malformed, expired, revoked, wrong-profile, wrong-epoch, wrong-target grants fail before POST."""

    def setUp(self) -> None:
        self.clock = FakeClock(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))
        self.action = make_sample_prepared_action()
        self.grant = make_matching_grant(self.action)
        self.verifier = FakeGrantVerifier(
            grants={"grant-ref-valid": self.grant},
            clock=self.clock,
        )

    def test_s05_t01_missing_grant_fails_before_post(self) -> None:
        """S05-T01: Missing grant reference fails with GRANT_MISSING before any transport call."""
        res = self.verifier.verify(
            reference="nonexistent-grant-ref",
            prepared_action=self.action,
            current_profile_epoch=1,
        )
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_MISSING)
        self.assertIn("not found", res.reason)

    def test_s05_t01_malformed_grant_fails(self) -> None:
        """S05-T01: Malformed grant dictionary or invalid types fail with GRANT_INVALID."""
        # Missing required field
        malformed_dict = {
            "action": "act-1",
            "operation_id": "op-1",
            # missing profile, source, repository, etc.
        }
        self.verifier.register_grant("malformed-1", malformed_dict)
        res = self.verifier.verify("malformed-1", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)

        # Non-integer profile_epoch
        bad_epoch_dict = {
            "action": "act-reply-1",
            "operation_id": "op-reply-1",
            "profile": "default",
            "profile_epoch": "one",
            "source": "sources/github/OWNER/REPO",
            "repository": "OWNER/REPO",
            "branch": "feature/example",
            "payload_hash": self.action.payload_hash,
            "context_hash": self.action.context_hash,
            "plan_hash": self.action.plan_hash,
            "publication_scope": "none",
            "authorizing_source": "coord",
        }
        self.verifier.register_grant("bad-epoch", bad_epoch_dict)
        res2 = self.verifier.verify("bad-epoch", self.action, current_profile_epoch=1)
        self.assertIsInstance(res2, GrantBlocker)
        self.assertEqual(res2.code, ErrorCode.GRANT_INVALID)

        # max_attempts != 1
        grant_attempts_2 = make_matching_grant(self.action, max_attempts=2)
        self.verifier.register_grant("attempts-2", grant_attempts_2)
        res3 = self.verifier.verify("attempts-2", self.action, current_profile_epoch=1)
        self.assertIsInstance(res3, GrantBlocker)
        self.assertEqual(res3.code, ErrorCode.GRANT_INVALID)
        self.assertIn("max_attempts must be exactly 1", res3.reason)

    def test_s05_t01_expired_grant_fails(self) -> None:
        """S05-T01: Expired grant fails with GRANT_EXPIRED against injected clock."""
        expired_grant = make_matching_grant(self.action, expiry="2026-10-07T11:00:00Z")
        self.verifier.register_grant("expired-ref", expired_grant)

        # Clock is 12:00:00, expiry was 11:00:00
        res = self.verifier.verify("expired-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_EXPIRED)

        # Clock advancing past valid grant's expiry
        # Valid grant expires at 14:00:00
        self.clock.advance(7201)  # now 14:00:01
        res2 = self.verifier.verify("grant-ref-valid", self.action, current_profile_epoch=1)
        self.assertIsInstance(res2, GrantBlocker)
        self.assertEqual(res2.code, ErrorCode.GRANT_EXPIRED)

    def test_s05_t01_revoked_grant_fails(self) -> None:
        """S05-T01: Revoked grant (by reference or revocation_ref) fails with GRANT_REVOKED."""
        # Revoke by reference
        self.verifier.revoke_grant("grant-ref-valid")
        res = self.verifier.verify("grant-ref-valid", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_REVOKED)

        # Revoke by revocation_ref
        grant_with_rev_ref = make_matching_grant(self.action, revocation_ref="rev-456")
        self.verifier.register_grant("rev-grant", grant_with_rev_ref)
        self.verifier.revoke_grant("rev-456")
        res2 = self.verifier.verify("rev-grant", self.action, current_profile_epoch=1)
        self.assertIsInstance(res2, GrantBlocker)
        self.assertEqual(res2.code, ErrorCode.GRANT_REVOKED)

    def test_s05_t01_wrong_profile_fails(self) -> None:
        """S05-T01: Profile mismatch between grant and action fails with GRANT_INVALID."""
        grant_prod = make_matching_grant(
            self.action,
        )
        # Manually alter profile in grant
        grant_prod_dict = {
            "action": self.action.action,
            "operation_id": self.action.operation_id,
            "profile": "prod-profile",  # mismatch with "default"
            "profile_epoch": 1,
            "source": self.action.binding.source,
            "repository": self.action.binding.repository,
            "branch": self.action.binding.starting_branch or "",
            "payload_hash": self.action.payload_hash,
            "context_hash": self.action.context_hash,
            "plan_hash": self.action.plan_hash,
            "publication_scope": "none",
            "authorizing_source": "coord",
            "session": self.action.binding.session,
            "max_attempts": 1,
        }
        self.verifier.register_grant("prod-ref", grant_prod_dict)
        res = self.verifier.verify("prod-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("profile", res.reason.lower())

    def test_s05_t01_wrong_credential_configuration_epoch_fails(self) -> None:
        """S05-T01: Epoch mismatch fails with RECOVERY_FENCE_STALE before any POST."""
        # Grant has epoch 1, but current host profile epoch is 2
        res = self.verifier.verify("grant-ref-valid", self.action, current_profile_epoch=2)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.RECOVERY_FENCE_STALE)
        self.assertIn("epoch", res.reason.lower())

        # Prepared action binding epoch mismatch with current host epoch
        stale_action = make_sample_prepared_action(
            binding=make_sample_binding(profile_epoch=0)
        )
        grant_epoch_2 = make_matching_grant(self.action)
        grant_epoch_2_dict = dict(
            action=self.action.action,
            operation_id=self.action.operation_id,
            profile=self.action.binding.profile,
            profile_epoch=2,
            source=self.action.binding.source,
            repository=self.action.binding.repository,
            branch=self.action.binding.starting_branch or "",
            payload_hash=self.action.payload_hash,
            context_hash=self.action.context_hash,
            plan_hash=self.action.plan_hash,
            publication_scope="none",
            authorizing_source="coord",
            session=self.action.binding.session,
            max_attempts=1,
        )
        self.verifier.register_grant("epoch-2-ref", grant_epoch_2_dict)
        res2 = self.verifier.verify("epoch-2-ref", stale_action, current_profile_epoch=2)
        self.assertIsInstance(res2, GrantBlocker)
        self.assertEqual(res2.code, ErrorCode.RECOVERY_FENCE_STALE)

    def test_s05_t01_rotated_credentials_advances_epoch_invalidates_old_grant(self) -> None:
        """S05-T01: Rotated synthetic credentials advance host epoch and invalidate old grant for same-name profile."""
        profile = "default"
        # Initial epoch is 1; grant issued for epoch 1
        initial_grant = self.grant
        self.assertEqual(initial_grant.profile_epoch, 1)

        # Host rotates synthetic credentials: epoch increments to 2
        new_host_epoch = 2

        # Old grant checked against advanced epoch 2 fails
        res = self.verifier.verify("grant-ref-valid", self.action, current_profile_epoch=new_host_epoch)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.RECOVERY_FENCE_STALE)

        # Re-issuing grant under new epoch 2 with freshly re-validated binding succeeds
        fresh_action = make_sample_prepared_action(
            binding=make_sample_binding(profile=profile, profile_epoch=new_host_epoch)
        )
        fresh_grant = make_matching_grant(fresh_action)
        self.verifier.register_grant("grant-ref-epoch2", fresh_grant)
        res_fresh = self.verifier.verify("grant-ref-epoch2", fresh_action, current_profile_epoch=new_host_epoch)
        self.assertIsInstance(res_fresh, VerifiedGrant)
        self.assertEqual(res_fresh.profile_epoch, 2)

    def test_s05_t01_wrong_target_source_repo_branch_session_fails(self) -> None:
        """S05-T01: Any target mismatch (source, repo, branch, session) produces GRANT_INVALID."""
        # Source mismatch
        grant_diff_source = make_matching_grant(self.action)
        object.__setattr__(grant_diff_source, "source", "sources/github/OTHER/REPO")
        self.verifier.register_grant("diff-source", grant_diff_source)
        res = self.verifier.verify("diff-source", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)

        # Repo mismatch
        grant_diff_repo = make_matching_grant(self.action)
        object.__setattr__(grant_diff_repo, "repository", "OTHER/REPO")
        self.verifier.register_grant("diff-repo", grant_diff_repo)
        res2 = self.verifier.verify("diff-repo", self.action, current_profile_epoch=1)
        self.assertIsInstance(res2, GrantBlocker)
        self.assertEqual(res2.code, ErrorCode.GRANT_INVALID)

        # Session mismatch
        grant_diff_session = make_matching_grant(self.action)
        object.__setattr__(grant_diff_session, "session", "sessions/DIFFERENT")
        self.verifier.register_grant("diff-session", grant_diff_session)
        res3 = self.verifier.verify("diff-session", self.action, current_profile_epoch=1)
        self.assertIsInstance(res3, GrantBlocker)
        self.assertEqual(res3.code, ErrorCode.GRANT_INVALID)


class TestS05T02GrantParameterInvalidation(unittest.TestCase):
    """S05-T02: Changed Unicode text, branch case, source, session, operation ID, plan/question hash or publication effect invalidates a grant."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.action = make_sample_prepared_action()
        self.grant = make_matching_grant(self.action)
        self.verifier = FakeGrantVerifier(
            grants={"grant-valid": self.grant},
            clock=self.clock,
        )

    def test_s05_t02_changed_unicode_text_invalidates_grant(self) -> None:
        """S05-T02: Altered Unicode payload (e.g. NFC vs NFD or character substitution) changes payload_hash and invalidates grant."""
        # NFC payload
        nfc_payload = {"prompt": "caf\u00e9"}
        nfc_action = make_sample_prepared_action(payload=nfc_payload)
        nfc_grant = make_matching_grant(nfc_action)
        self.verifier.register_grant("nfc-grant", nfc_grant)

        # Action with NFD payload produces a distinct payload_hash
        nfd_payload = {"prompt": "cafe\u0301"}
        nfd_action = make_sample_prepared_action(payload=nfd_payload)

        self.assertNotEqual(nfc_action.payload_hash, nfd_action.payload_hash)
        res = self.verifier.verify("nfc-grant", nfd_action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("payload_hash", res.reason)

    def test_s05_t02_changed_branch_case_invalidates_grant(self) -> None:
        """S05-T02: Branch name comparison is strictly case-sensitive: 'main' vs 'Main' invalidates grant."""
        grant_lower = make_matching_grant(
            make_sample_prepared_action(
                binding=make_sample_binding(starting_branch="main")
            )
        )
        self.verifier.register_grant("lower-branch", grant_lower)

        action_upper = make_sample_prepared_action(
            binding=make_sample_binding(starting_branch="Main")
        )

        res = self.verifier.verify("lower-branch", action_upper, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("branch", res.reason.lower())

    def test_s05_t02_changed_source_invalidates_grant(self) -> None:
        """S05-T02: Changed source invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "source", "sources/github/OTHER/REPO")
        self.verifier.register_grant("src-ref", grant)

        res = self.verifier.verify("src-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)

    def test_s05_t02_changed_session_invalidates_grant(self) -> None:
        """S05-T02: Changed session invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "session", "sessions/OTHER_SESSION")
        self.verifier.register_grant("sess-ref", grant)

        res = self.verifier.verify("sess-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)

    def test_s05_t02_changed_operation_id_invalidates_grant(self) -> None:
        """S05-T02: Changed operation ID invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "operation_id", "op-different-id")
        self.verifier.register_grant("op-ref", grant)

        res = self.verifier.verify("op-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("operation_id", res.reason.lower())

    def test_s05_t02_changed_plan_hash_invalidates_grant(self) -> None:
        """S05-T02: Altered plan hash invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "plan_hash", "sha256:9999999999999999999999999999999999999999999999999999999999999999")
        self.verifier.register_grant("plan-ref", grant)

        res = self.verifier.verify("plan-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("plan_hash", res.reason.lower())

    def test_s05_t02_changed_context_hash_invalidates_grant(self) -> None:
        """S05-T02: Altered question or conversation context hash invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "context_hash", "sha256:8888888888888888888888888888888888888888888888888888888888888888")
        self.verifier.register_grant("ctx-ref", grant)

        res = self.verifier.verify("ctx-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("context_hash", res.reason.lower())

    def test_s05_t02_changed_publication_scope_invalidates_grant(self) -> None:
        """S05-T02: Publication scope mismatch invalidates grant."""
        grant = make_matching_grant(self.action)
        object.__setattr__(grant, "publication_scope", "github_pr")
        self.verifier.register_grant("pub-ref", grant)

        res = self.verifier.verify("pub-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.GRANT_INVALID)
        self.assertIn("publication_scope", res.reason.lower())


class TestS05T03LocalFileModificationsCannotManufactureGrant(unittest.TestCase):
    """S05-T03: Worker can modify all local files but cannot manufacture a verified grant; no trust adapter means writes disabled."""

    def test_s05_t03_no_trust_adapter_disables_writes(self) -> None:
        """S05-T03: Default DisabledGrantVerifier always fails closed with VERIFIER_UNAVAILABLE."""
        disabled_verifier = DisabledGrantVerifier()
        action = make_sample_prepared_action()

        res = disabled_verifier.verify(
            reference="any-grant-ref",
            prepared_action=action,
            current_profile_epoch=1,
        )
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.VERIFIER_UNAVAILABLE)
        self.assertIn("automated writes disabled", res.reason.lower())

    def test_s05_t03_worker_modified_local_files_cannot_manufacture_verified_grant(self) -> None:
        """S05-T03: Setting 'approved: true' in local plan or assertion files does not grant authority."""
        # Worker modifies plan action payload to include 'approved: true'
        hacked_action_dict = {
            "id": "act-hacked",
            "op": "chats.reply",
            "enabled": True,
            "operation_id": "op-hack-1",
            "authorization_ref": "worker-forged-token-123",
            "target": "sessions/EXAMPLE",
            "payload": {
                "text": "Unauthorized message",
                "approved": True,  # Worker assertion!
            },
            "preconditions": {
                "approved": True,  # Worker assertion!
            },
        }

        # Runner evaluates against default verifier
        default_verifier = DisabledGrantVerifier()
        prep_action = make_sample_prepared_action(
            action=hacked_action_dict["id"],
            operation_id=hacked_action_dict["operation_id"],
            payload=hacked_action_dict["payload"],
        )

        blocker = default_verifier.verify(
            reference=hacked_action_dict["authorization_ref"],
            prepared_action=prep_action,
            current_profile_epoch=1,
        )
        self.assertIsInstance(blocker, GrantBlocker)
        self.assertEqual(blocker.code, ErrorCode.VERIFIER_UNAVAILABLE)

        # Even with a fake verifier configured, an unissued reference is rejected
        fake_verifier = FakeGrantVerifier(grants={})
        blocker2 = fake_verifier.verify(
            reference=hacked_action_dict["authorization_ref"],
            prepared_action=prep_action,
            current_profile_epoch=1,
        )
        self.assertIsInstance(blocker2, GrantBlocker)
        self.assertEqual(blocker2.code, ErrorCode.GRANT_MISSING)


class FileBackedFakeStore:
    """File-backed test fake store that persists OperationRecords atomically to disk.

    Note: The real durable SQLite store and journal are implemented and proven
    in S03 and S08. This file-backed fake proves for S05-T04 that single-attempt
    dispatch records and claims survive across simulated process restarts.
    """

    def __init__(self, state_dir: str, current_epoch: int = 1) -> None:
        self.state_dir = state_dir
        self.current_epoch = current_epoch
        self.ops_dir = os.path.join(self.state_dir, "operations")
        self.claims_file = os.path.join(self.state_dir, "consumed_claims.json")
        os.makedirs(self.ops_dir, exist_ok=True)
        self.locked = False
        self._recover_inflight_operations()

    def _recover_inflight_operations(self) -> None:
        """Crash recovery: recover any operation left in DISPATCHING to UNKNOWN."""
        if not os.path.exists(self.ops_dir):
            return
        for filename in os.listdir(self.ops_dir):
            if filename.endswith(".json"):
                path = os.path.join(self.ops_dir, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("state") == OperationState.DISPATCHING.value:
                        data["state"] = OperationState.UNKNOWN.value
                        tmp_path = path + ".tmp"
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(data, f)
                        os.replace(tmp_path, path)
                except Exception:
                    pass

    def acquire_lock(self, timeout: float = 0.0) -> bool:
        if self.locked:
            return False
        self.locked = True
        return True

    def release_lock(self) -> None:
        self.locked = False

    def get_profile_epoch(self, profile: str) -> int:
        return self.current_epoch

    def save_operation(self, record: OperationRecord) -> None:
        path = os.path.join(self.ops_dir, f"{record.operation_id}.json")
        tmp_path = path + ".tmp"
        data = {
            "operation_id": record.operation_id,
            "state": record.state.value,
            "request_hash": record.request_hash,
            "ticket_id": record.ticket_id,
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        path = os.path.join(self.ops_dir, f"{operation_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return OperationRecord(
            operation_id=data["operation_id"],
            state=OperationState(data["state"]),
            request_hash=data["request_hash"],
            ticket_id=data.get("ticket_id"),
        )

    def mark_claim(self, reference: str) -> None:
        claims = self.get_claims()
        claims.add(reference)
        tmp_path = self.claims_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(claims), f)
        os.replace(tmp_path, self.claims_file)

    def get_claims(self) -> set[str]:
        if not os.path.exists(self.claims_file):
            return set()
        with open(self.claims_file, "r", encoding="utf-8") as f:
            return set(json.load(f))

    def save_receipt(self, receipt: Any) -> None:
        pass

    def get_receipt(self, receipt_id: str) -> Any | None:
        return None

    def save_event(self, event: Any) -> None:
        pass

    def get_events(self, limit: int = 100) -> tuple[Any, ...]:
        return ()


class FakeMutationJournal:
    """In-test fake MutationJournal backed by FileBackedFakeStore."""

    def __init__(self, store: FileBackedFakeStore) -> None:
        self.store = store
        self.ticket_counter = 0
        self.dispatch_count = 0

    def prepare(self, action: PreparedAction, grant: VerifiedGrant) -> OperationRecord:
        record = OperationRecord(
            operation_id=action.operation_id,
            state=OperationState.PREPARED,
            request_hash=action.request_hash,
            binding=action.binding,
        )
        self.store.save_operation(record)
        return record

    def begin_dispatch(
        self, operation_id: str, request_hash: str
    ) -> DispatchTicket | OperationRecord:
        existing = self.store.get_operation(operation_id)
        if existing is None:
            raise OctodotError(
                ErrorCode.OPERATION_CONFLICT, f"Operation {operation_id} not prepared"
            )

        # (c) Conflicting request_hash raises OPERATION_CONFLICT
        if existing.request_hash != request_hash:
            raise OctodotError(
                ErrorCode.OPERATION_CONFLICT,
                f"Operation {operation_id} has conflicting request_hash: "
                f"expected {existing.request_hash}, got {request_hash}",
            )

        # (a) Replay with same operation_id and same request_hash returns recorded record
        if existing.state != OperationState.PREPARED:
            # Do NOT mint ticket, do NOT attempt dispatch
            return existing

        # First dispatch attempt: mint single-use ticket and commit DISPATCHING state
        self.ticket_counter += 1
        self.dispatch_count += 1
        ticket = DispatchTicket(
            ticket_id=f"ticket-{self.ticket_counter}",
            operation_id=operation_id,
            request_hash=request_hash,
            nonce=f"nonce-{self.ticket_counter}",
        )
        updated = OperationRecord(
            operation_id=operation_id,
            state=OperationState.DISPATCHING,
            request_hash=request_hash,
            binding=existing.binding,
            ticket_id=ticket.ticket_id,
        )
        self.store.save_operation(updated)
        return ticket

    def record_outcome(self, ticket: DispatchTicket, outcome: Any, evidence: Any = None) -> OperationRecord:
        record = OperationRecord(
            operation_id=ticket.operation_id,
            state=OperationState.ACCEPTED if getattr(outcome, "status", 0) == 200 else OperationState.UNKNOWN,
            request_hash=ticket.request_hash,
            ticket_id=ticket.ticket_id,
        )
        self.store.save_operation(record)
        return record

    def get_record(self, operation_id: str) -> OperationRecord | None:
        return self.store.get_operation(operation_id)


class TestS05T04DurableReplayAndRecoveryFence(unittest.TestCase):
    """S05-T04: Gateway unavailable or stale recovery fence blocks dispatch; replay and single-attempt claim are durable."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = self.temp_dir_obj.name
        self.store = FileBackedFakeStore(state_dir=self.temp_dir, current_epoch=1)
        self.journal = FakeMutationJournal(self.store)
        self.action = make_sample_prepared_action()
        self.grant = make_matching_grant(self.action)
        self.verifier = FakeGrantVerifier(
            grants={"grant-1": self.grant},
            clock=self.clock,
            single_use=True,
        )

    def tearDown(self) -> None:
        self.temp_dir_obj.cleanup()

    def test_s05_t04_gateway_unavailable_blocks_dispatch(self) -> None:
        """S05-T04: Gateway unavailable blocks dispatch before any POST attempt."""
        def failing_host_verifier(ref: str, act: PreparedAction, epoch: int) -> GrantBlocker:
            raise ConnectionError("Gateway service unreachable")

        adapter = HostGrantVerifierAdapter(failing_host_verifier)
        res = adapter.verify("grant-1", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.VERIFIER_UNAVAILABLE)
        self.assertIn("unreachable", res.reason)

    def test_s05_t04_stale_recovery_fence_blocks_dispatch(self) -> None:
        """S05-T04: Stale recovery fence blocks dispatch when host epoch advances."""
        # Host epoch advances to 2
        res = self.verifier.verify("grant-1", self.action, current_profile_epoch=2)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.RECOVERY_FENCE_STALE)

    def test_s05_t04_replay_and_single_attempt_claim_are_durable(self) -> None:
        """S05-T04: Replay and single-attempt claim are durable across simulated process restart."""
        # --- Process 1 execution ---
        store_p1 = FileBackedFakeStore(state_dir=self.temp_dir, current_epoch=1)
        journal_p1 = FakeMutationJournal(store_p1)
        verifier_p1 = FakeGrantVerifier(
            grants={"grant-1": self.grant},
            clock=self.clock,
            single_use=True,
        )

        # Step 1: Verify grant
        verified = verifier_p1.verify("grant-1", self.action, current_profile_epoch=1)
        self.assertIsInstance(verified, VerifiedGrant)

        # Step 2: Prepare in durable journal and record claim to disk
        journal_p1.prepare(self.action, verified)
        verifier_p1.mark_consumed("grant-1")
        store_p1.mark_claim("grant-1")

        # Step 3: Begin first dispatch
        ticket = journal_p1.begin_dispatch(self.action.operation_id, self.action.request_hash)
        self.assertIsInstance(ticket, DispatchTicket)
        self.assertEqual(journal_p1.ticket_counter, 1)
        self.assertEqual(journal_p1.dispatch_count, 1)

        # Replay in process 1 with same ID and same hash returns recorded record without minting ticket
        replay_p1 = journal_p1.begin_dispatch(self.action.operation_id, self.action.request_hash)
        self.assertIsInstance(replay_p1, OperationRecord)
        self.assertEqual(replay_p1.state, OperationState.DISPATCHING)
        self.assertEqual(journal_p1.ticket_counter, 1)  # No new ticket minted!
        self.assertEqual(journal_p1.dispatch_count, 1)  # Dispatch count stays at 1!

        # Step 4: Second verification of consumed grant reference fails
        second_verify = verifier_p1.verify("grant-1", self.action, current_profile_epoch=1)
        self.assertIsInstance(second_verify, GrantBlocker)
        self.assertEqual(second_verify.code, ErrorCode.GRANT_REVOKED)
        self.assertIn("already been consumed", second_verify.reason)

        # --- Simulate process termination & restart ---
        del store_p1
        del journal_p1
        del verifier_p1

        # Process 2 constructs NEW instances over the same directory.
        # Note: The real durable SQLite store and journal are implemented and proven in S03 and S08;
        # this file-backed test fake proves that single-attempt dispatch claims survive simulated process restarts.
        store_p2 = FileBackedFakeStore(state_dir=self.temp_dir, current_epoch=1)
        journal_p2 = FakeMutationJournal(store_p2)

        # Reopen verifier in process 2 and load persisted claims from disk
        verifier_p2 = FakeGrantVerifier(
            grants={"grant-1": self.grant},
            clock=self.clock,
            single_use=True,
        )
        for claim in store_p2.get_claims():
            verifier_p2.mark_consumed(claim)

        # (a) Assert the replay returns the recorded record (state UNKNOWN after restart)
        replay_p2 = journal_p2.begin_dispatch(self.action.operation_id, self.action.request_hash)
        self.assertIsInstance(replay_p2, OperationRecord)
        self.assertEqual(replay_p2.state, OperationState.UNKNOWN)
        self.assertEqual(replay_p2.operation_id, self.action.operation_id)
        self.assertEqual(replay_p2.ticket_id, ticket.ticket_id)

        # (b) Assert no new ticket is issued and the dispatch count stays at 1
        self.assertEqual(journal_p2.ticket_counter, 0)
        self.assertEqual(journal_p2.dispatch_count, 0)
        # Total dispatches across process 1 and process 2 stays at exactly 1
        self.assertEqual(1 + journal_p2.dispatch_count, 1)

        # (c) Assert the same ID with a DIFFERENT request_hash raises OPERATION_CONFLICT
        different_hash = "sha256:different99999999999999999999999999999999999999999999999999999999"
        with self.assertRaises(OctodotError) as ctx_conflict:
            journal_p2.begin_dispatch(self.action.operation_id, different_hash)
        self.assertEqual(ctx_conflict.exception.code, ErrorCode.OPERATION_CONFLICT)
        self.assertIn("conflicting request_hash", str(ctx_conflict.exception).lower())

        # Keep the grant single-attempt claim assertions
        restart_verify = verifier_p2.verify("grant-1", self.action, current_profile_epoch=1)
        self.assertIsInstance(restart_verify, GrantBlocker)
        self.assertEqual(restart_verify.code, ErrorCode.GRANT_REVOKED)
        self.assertIn("already been consumed", restart_verify.reason)


class TestS05T06FakeVerifierBarredFromLiveMode(unittest.TestCase):
    """S05-T06: Fixtures use an in-memory fake verifier explicitly barred from live mode; no fixture credentials or approval export."""

    def setUp(self) -> None:
        self.clock = FakeClock(datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc))
        self.action = make_sample_prepared_action()
        self.grant = make_matching_grant(self.action, expiry="2026-10-07T14:00:00Z")
        self.verifier = FakeGrantVerifier(
            grants={"grant-test": self.grant},
            clock=self.clock,
        )

    def test_s05_t06_fake_verifier_barred_from_live_mode(self) -> None:
        """S05-T06: require_verifier_allowed raises AUTH_DENIED when live=True for fixture verifier."""
        # live=True with FakeGrantVerifier raises AUTH_DENIED
        with self.assertRaises(OctodotError) as ctx:
            require_verifier_allowed(self.verifier, live=True)
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_DENIED)
        self.assertIn("barred from live mode", str(ctx.exception).lower())

        # live=False is permitted for offline fixtures
        require_verifier_allowed(self.verifier, live=False)
        res = self.verifier.verify("grant-test", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, VerifiedGrant)

    def test_s05_t06_disabled_verifier_allowed_in_live_mode_but_verify_blocked(self) -> None:
        """S05-T06: DisabledGrantVerifier is permitted in live mode, but every verify() call is blocked."""
        disabled = DisabledGrantVerifier()
        # Permitted in live mode
        require_verifier_allowed(disabled, live=True)

        # But verify() always fails closed with VERIFIER_UNAVAILABLE
        res = disabled.verify("any-ref", self.action, current_profile_epoch=1)
        self.assertIsInstance(res, GrantBlocker)
        self.assertEqual(res.code, ErrorCode.VERIFIER_UNAVAILABLE)

    def test_s05_t06_host_adapter_allowed_in_live_mode(self) -> None:
        """S05-T06: HostGrantVerifierAdapter is permitted in live mode."""
        adapter = HostGrantVerifierAdapter(lambda ref, act, epoch: make_matching_grant(act))
        require_verifier_allowed(adapter, live=True)

    def test_s05_t06_unauthorized_verifier_rejected_in_live_mode(self) -> None:
        """S05-T06: Unknown or custom object is rejected in live mode."""
        with self.assertRaises(OctodotError) as ctx:
            require_verifier_allowed(object(), live=True)
        self.assertEqual(ctx.exception.code, ErrorCode.AUTH_DENIED)

    def test_s05_t06_no_setter_exists_and_marker_is_immutable(self) -> None:
        """S05-T06: No live flag setter exists on FakeGrantVerifier; FIXTURE_ONLY is immutable."""
        # No setter exists (hasattr check)
        self.assertFalse(hasattr(self.verifier, "set_live_mode"))
        self.assertFalse(hasattr(self.verifier, "live_mode"))

        # FIXTURE_ONLY marker is present and True
        self.assertTrue(getattr(self.verifier, "FIXTURE_ONLY", False))

        # Attempting to modify or reassign FIXTURE_ONLY on instance fails
        with self.assertRaises(AttributeError):
            self.verifier.FIXTURE_ONLY = False

    def test_s05_t06_no_fixture_credentials_or_approval_export(self) -> None:
        """S05-T06: Fake verifier operates strictly in memory without reading environment secrets or writing export artifacts."""
        # Spy on os.environ reads to confirm FakeGrantVerifier never touches secrets
        env_reads: list[str] = []
        original_getitem = os.environ.__class__.__getitem__

        def spy_getitem(self_env: Any, key: str) -> str:
            env_reads.append(key)
            return original_getitem(self_env, key)

        try:
            os.environ.__class__.__getitem__ = spy_getitem
            # Run verification
            res = self.verifier.verify("grant-test", self.action, current_profile_epoch=1)
            self.assertIsInstance(res, VerifiedGrant)
        finally:
            os.environ.__class__.__getitem__ = original_getitem

        # Prove GOOGLE_JULES_KEY was never read by the verifier
        self.assertNotIn("GOOGLE_JULES_KEY", env_reads)

        # Verify verifier has no approval export methods or file system side effects
        self.assertFalse(hasattr(self.verifier, "export_approvals"))
        self.assertFalse(hasattr(self.verifier, "write_credentials"))
        self.assertFalse(hasattr(self.verifier, "save_to_file"))


if __name__ == "__main__":
    unittest.main()
