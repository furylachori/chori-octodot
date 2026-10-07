"""Contracts and schema test suite for octodot.

Covers test cases S01-T01 through S01-T05.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import unittest
from typing import Any

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from octodot.contracts import (
    CANONICAL_ENCODING_VERSION,
    LIVE_INVOCATION_DEFAULTS,
    OPERATION_INVENTORY,
    ArtifactsExportPatchArgs,
    ArtifactsExportPatchResult,
    CapabilitiesInspectArgs,
    CapabilitiesInspectResult,
    CapabilityClassification,
    ChatsCollectArgs,
    ChatsCollectResult,
    ChatsReplyArgs,
    ChatsReplyResult,
    CredentialSource,
    EventsAckArgs,
    EventsAckResult,
    EventsReadArgs,
    EventsReadResult,
    HealthcheckArgs,
    HealthcheckResult,
    InventoryCollectArgs,
    InventoryCollectResult,
    OperationClassification,
    OperationsReconcileArgs,
    OperationsReconcileResult,
    PlansApproveArgs,
    PlansApproveResult,
    PublicationVerifyArgs,
    PublicationVerifyResult,
    ResultBuilder,
    SessionInspectArgs,
    SessionInspectResult,
    SuggestionsCollectArgs,
    SuggestionsCollectResult,
    TasksCreateArgs,
    TasksCreateResult,
    TicketAuthority,
    WaitArgs,
    WaitResult,
    binding_hash,
    canonical_bytes,
    canonical_hash,
    check_execution_eligibility,
    compute_plan_hash,
    context_hash,
    load_strict_json,
    request_hash,
    validate_plan,
    validate_result,
)
from octodot.errors import (
    EXIT_FATAL_READ_OR_LOCAL,
    EXIT_INTERRUPTED,
    EXIT_MUTATION_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL_OR_UNSUPPORTED,
    EXIT_WAITING,
    ErrorCode,
    OctodotError,
    combine_exit_codes,
)
from octodot.models import (
    ActionResult,
    ActionResultStatus,
    ActivityRecord,
    Binding,
    CandidateBundle,
    Capability,
    Coverage,
    DispatchTicket,
    Event,
    LifecycleBucket,
    MutationResponse,
    OperationRecord,
    OperationState,
    PreparedAction,
    Receipt,
    SessionRecord,
    SourceRecord,
    TransportOutcome,
    VerifiedGrant,
    is_legal_operation_transition,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_EXAMPLES_DIR = os.path.join(_REPO_ROOT, "examples")
_SCHEMAS_DIR = os.path.join(_REPO_ROOT, "schemas")


class SpyCredentialSource:
    """Credential source spy that records every access attempt."""

    def __init__(self) -> None:
        self.accesses: list[str] = []

    def get_credential(self, profile: str) -> str | None:
        self.accesses.append(profile)
        return "synthetic-token-never-used"

    def was_accessed(self) -> bool:
        return len(self.accesses) > 0

    def access_count(self) -> int:
        return len(self.accesses)


class TestS01T01SchemaExamples(unittest.TestCase):
    """S01-T01: Schema examples include a read-only plan, a partial result and disabled templates."""

    def setUp(self) -> None:
        self.read_only_path = os.path.join(_EXAMPLES_DIR, "read_only_plan.json")
        self.partial_result_path = os.path.join(_EXAMPLES_DIR, "partial_result.json")
        self.disabled_reply_path = os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json")
        self.disabled_create_path = os.path.join(_EXAMPLES_DIR, "disabled_create_template.json")
        self.disabled_approve_path = os.path.join(_EXAMPLES_DIR, "disabled_approve_template.json")

    def test_s01_t01_read_only_plan_structurally_valid(self) -> None:
        """S01-T01: read_only_plan.json validates structurally with matching plan_hash."""
        with open(self.read_only_path, "rb") as f:
            plan = load_strict_json(f.read())
        validate_plan(plan)
        computed = compute_plan_hash(plan)
        self.assertEqual(plan["plan_hash"], computed)

    def test_s01_t01_partial_result_structurally_valid(self) -> None:
        """S01-T01: partial_result.json validates structurally with status and exit code."""
        with open(self.partial_result_path, "rb") as f:
            result = load_strict_json(f.read())
        validate_result(result)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["exit_code"], EXIT_PARTIAL_OR_UNSUPPORTED)
        self.assertFalse(result["coverage"]["complete"])

    def test_s01_t01_disabled_reply_template_structurally_valid(self) -> None:
        """S01-T01: disabled_reply_template.json validates structurally with matching plan_hash."""
        with open(self.disabled_reply_path, "rb") as f:
            plan = load_strict_json(f.read())
        validate_plan(plan)
        computed = compute_plan_hash(plan)
        self.assertEqual(plan["plan_hash"], computed)
        self.assertFalse(plan["actions"][0]["enabled"])

    def test_s01_t01_disabled_create_template_structurally_valid(self) -> None:
        """S01-T01: disabled_create_template.json validates structurally with matching plan_hash."""
        with open(self.disabled_create_path, "rb") as f:
            plan = load_strict_json(f.read())
        validate_plan(plan)
        computed = compute_plan_hash(plan)
        self.assertEqual(plan["plan_hash"], computed)
        self.assertFalse(plan["actions"][0]["enabled"])

    def test_s01_t01_disabled_approve_template_structurally_valid(self) -> None:
        """S01-T01: disabled_approve_template.json validates structurally with matching plan_hash."""
        with open(self.disabled_approve_path, "rb") as f:
            plan = load_strict_json(f.read())
        validate_plan(plan)
        computed = compute_plan_hash(plan)
        self.assertEqual(plan["plan_hash"], computed)
        self.assertFalse(plan["actions"][0]["enabled"])

    def test_s01_t01_jsonschema_cross_validation_if_installed(self) -> None:
        """S01-T01: Cross-check examples against JSON Schema draft 2020-12 if jsonschema is available."""
        try:
            import jsonschema  # type: ignore[import-untyped]
        except ImportError:
            self.skipTest("jsonschema is not installed (dev dependency)")

        with open(os.path.join(_SCHEMAS_DIR, "jules-controller.plan.v1.schema.json"), "r", encoding="utf-8") as f:
            plan_schema = json.load(f)
        with open(os.path.join(_SCHEMAS_DIR, "jules-controller.result.v1.schema.json"), "r", encoding="utf-8") as f:
            result_schema = json.load(f)

        for path in (self.read_only_path, self.disabled_reply_path, self.disabled_create_path, self.disabled_approve_path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            jsonschema.validate(instance=data, schema=plan_schema)

        with open(self.partial_result_path, "r", encoding="utf-8") as f:
            res_data = json.load(f)
        jsonschema.validate(instance=res_data, schema=result_schema)


class TestS01T02Rejections(unittest.TestCase):
    """S01-T02: Reject duplicate keys, unknown input fields, invalid types, nonfinite numbers, etc."""

    def _make_valid_plan(self) -> dict[str, Any]:
        with open(os.path.join(_EXAMPLES_DIR, "read_only_plan.json"), "rb") as f:
            return load_strict_json(f.read())

    def test_s01_t02_reject_duplicate_keys(self) -> None:
        """S01-T02: Reject duplicate keys in JSON documents."""
        raw = b'{"key": 1, "key": 2}'
        with self.assertRaises(OctodotError) as ctx:
            load_strict_json(raw)
        self.assertEqual(ctx.exception.code, ErrorCode.DUPLICATE_KEY)

    def test_s01_t02_reject_nonfinite_numbers(self) -> None:
        """S01-T02: Reject NaN, Infinity, -Infinity in JSON documents."""
        for val in (b'{"num": NaN}', b'{"num": Infinity}', b'{"num": -Infinity}'):
            with self.subTest(val=val):
                with self.assertRaises(OctodotError) as ctx:
                    load_strict_json(val)
                self.assertEqual(ctx.exception.code, ErrorCode.NONFINITE_NUMBER)

    def test_s01_t02_reject_oversized_input(self) -> None:
        """S01-T02: Reject input exceeding byte cap."""
        oversized = b"{" + b" " * 1_048_580 + b"}"
        with self.assertRaises(OctodotError) as ctx:
            load_strict_json(oversized, max_bytes=1_048_576)
        self.assertEqual(ctx.exception.code, ErrorCode.OVERSIZED_INPUT)

    def test_s01_t02_reject_non_utf8_input(self) -> None:
        """S01-T02: Reject non-UTF-8 input."""
        bad_bytes = b'{"msg": "\xff\xfe"}'
        with self.assertRaises(OctodotError) as ctx:
            load_strict_json(bad_bytes)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s01_t02_reject_top_level_non_object(self) -> None:
        """S01-T02: Reject top-level JSON array or primitive."""
        for bad in (b'[1, 2, 3]', b'"string"', b'123'):
            with self.subTest(bad=bad):
                with self.assertRaises(OctodotError) as ctx:
                    load_strict_json(bad)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s01_t02_reject_unknown_top_level_field(self) -> None:
        """S01-T02: Reject unknown top-level field in plan."""
        plan = self._make_valid_plan()
        plan["unknown_root_key"] = "forbidden"
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_FIELD)

    def test_s01_t02_reject_unknown_action_field(self) -> None:
        """S01-T02: Reject unknown field in action object."""
        plan = self._make_valid_plan()
        plan["actions"][0]["unrecognized_action_prop"] = 123
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_FIELD)

    def test_s01_t02_reject_invalid_types(self) -> None:
        """S01-T02: Reject invalid field types (e.g. integer where string expected)."""
        plan = self._make_valid_plan()
        plan["profile"] = 12345  # Must be string
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s01_t02_reject_duplicate_action_ids(self) -> None:
        """S01-T02: Reject duplicate action IDs."""
        plan = self._make_valid_plan()
        # Duplicate first action ID on second action
        plan["actions"][1]["id"] = plan["actions"][0]["id"]
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.DUPLICATE_KEY)

    def test_s01_t02_reject_forward_reference(self) -> None:
        """S01-T02: Reject action referencing a subsequent action."""
        plan = self._make_valid_plan()
        # Make action 0 reference action 1 (forward reference)
        plan["actions"][0]["params"] = {
            "session": {"from": "act-chats", "select": "candidate_bundle"}
        }
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_invalid_reference_nonexistent_action(self) -> None:
        """S01-T02: Reject reference to an action ID that does not exist."""
        plan = self._make_valid_plan()
        plan["actions"][1]["params"] = {
            "session": {"from": "act-ghost", "select": "sessions"}
        }
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_invalid_reference_unknown_selection(self) -> None:
        """S01-T02: Reject reference using a selection not in allowed_selections."""
        plan = self._make_valid_plan()
        plan["actions"][1]["params"] = {
            "session": {"from": "act-inventory", "select": "nonexistent_selection"}
        }
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_self_reference(self) -> None:
        """S01-T02: Reject action referencing its own output."""
        plan = self._make_valid_plan()
        plan["actions"][0]["params"] = {
            "session": {"from": "act-inventory", "select": "sessions"}
        }
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_reference_to_mutation_action(self) -> None:
        """S01-T02: Reject reference to a mutation action."""
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())
        # Append a read action that tries to reference the mutation
        plan["actions"].append({
            "id": "act-inspect-after",
            "op": "session.inspect",
            "params": {
                "session": {"from": "act-reply", "select": "something"}
            }
        })
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_arbitrary_jsonpath_and_expressions(self) -> None:
        """S01-T02: Reject arbitrary JSONPath expressions in read parameters."""
        for expr in ("$.sessions[0].name", "$[0]", "jsonpath:$.title"):
            with self.subTest(expr=expr):
                plan = self._make_valid_plan()
                plan["actions"][1]["params"] = {"session": expr}
                plan["plan_hash"] = compute_plan_hash(plan)
                with self.assertRaises(OctodotError) as ctx:
                    validate_plan(plan)
                self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REFERENCE)

    def test_s01_t02_reject_dynamic_mutation_payload_and_target(self) -> None:
        """S01-T02: Reject dynamic references inside mutation target or payload."""
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())

        # Test dynamic target
        plan["actions"][0]["target"] = {"from": "act-prev", "select": "session"}
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.DYNAMIC_MUTATION_TARGET)

        # Test dynamic payload
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())
        plan["actions"][0]["payload"] = {"text": {"from": "act-prev", "select": "msg"}}
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.DYNAMIC_MUTATION_TARGET)

        # Test JSONPath in payload
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())
        plan["actions"][0]["payload"] = {"text": "$.messages[0].text"}
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.DYNAMIC_MUTATION_TARGET)

    def test_s01_t02_reject_max_posts_greater_than_zero_in_read_only_mode(self) -> None:
        """S01-T02: validate_plan rejects max_posts > 0 for read_only mode and accepts max_posts == 0."""
        plan = self._make_valid_plan()
        plan["execution"]["mode"] = "read_only"
        plan["limits"]["max_posts"] = 0
        plan["plan_hash"] = compute_plan_hash(plan)
        validate_plan(plan)  # Accepts 0

        plan["limits"]["max_posts"] = 1
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s01_t02_reject_max_posts_greater_than_zero_in_authorized_get_mode(self) -> None:
        """S01-T02: validate_plan rejects max_posts > 0 for authorized_get mode and accepts max_posts == 0."""
        plan = self._make_valid_plan()
        plan["execution"]["mode"] = "authorized_get"
        plan["limits"]["max_posts"] = 0
        plan["plan_hash"] = compute_plan_hash(plan)
        validate_plan(plan)  # Accepts 0

        plan["limits"]["max_posts"] = 1
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)

    def test_s01_t02_reject_max_posts_greater_than_one_in_mutation_mode(self) -> None:
        """S01-T02: validate_plan rejects max_posts > 1 for mutation mode and accepts max_posts in {0, 1}."""
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())

        plan["limits"]["max_posts"] = 1
        plan["plan_hash"] = compute_plan_hash(plan)
        validate_plan(plan)  # Accepts 1

        plan["limits"]["max_posts"] = 0
        plan["plan_hash"] = compute_plan_hash(plan)
        validate_plan(plan)  # Accepts 0

        plan["limits"]["max_posts"] = 2
        plan["plan_hash"] = compute_plan_hash(plan)
        with self.assertRaises(OctodotError) as ctx:
            validate_plan(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_INPUT)


class TestS01T03ExecutionEligibility(unittest.TestCase):
    """S01-T03: Disabled and placeholder-bearing mutation templates fail execution eligibility before credential access."""

    def test_s01_t03_signature_takes_no_credentials_or_transport(self) -> None:
        """S01-T03: Structurally assert check_execution_eligibility takes only the plan parameter."""
        sig = inspect.signature(check_execution_eligibility)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["plan"])

    def test_s01_t03_disabled_mutation_fails_before_credentials(self) -> None:
        """S01-T03: Template with enabled=False fails with TEMPLATE_DISABLED."""
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())

        with self.assertRaises(OctodotError) as ctx:
            check_execution_eligibility(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.TEMPLATE_DISABLED)

    def test_s01_t03_placeholder_bearing_template_fails_before_credentials(self) -> None:
        """S01-T03: Enabled template bearing placeholder tokens fails with PLACEHOLDER_PRESENT."""
        with open(os.path.join(_EXAMPLES_DIR, "disabled_reply_template.json"), "rb") as f:
            plan = load_strict_json(f.read())

        # Enable the action so it passes the disabled check, but retains placeholders
        plan["actions"][0]["enabled"] = True

        with self.assertRaises(OctodotError) as ctx:
            check_execution_eligibility(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.PLACEHOLDER_PRESENT)

    def test_s01_t03_replace_me_and_todo_tokens_fail_eligibility(self) -> None:
        """S01-T03: Plan containing REPLACE_ME or TODO tokens fails with PLACEHOLDER_PRESENT."""
        with open(os.path.join(_EXAMPLES_DIR, "read_only_plan.json"), "rb") as f:
            plan = load_strict_json(f.read())

        plan["actions"][0]["params"]["scope"] = "REPLACE_ME"

        with self.assertRaises(OctodotError) as ctx:
            check_execution_eligibility(plan)
        self.assertEqual(ctx.exception.code, ErrorCode.PLACEHOLDER_PRESENT)


class TestS01T04CanonicalHashGoldenVectors(unittest.TestCase):
    """S01-T04: Golden canonical-hash vectors cover Unicode, newline differences, object key order, branch case, and volatile metadata."""

    # Hardcoded expected SHA-256 digests
    EXPECTED_UNICODE_NFC = "sha256:42d3cbf59fdccced04e5dff14433fb52d34d58e385e9770ffd896ff517d63b92"
    EXPECTED_UNICODE_NFD = "sha256:9b53287cd41955684903378d2b1b4a3ddea9d80d67dcd026319a7c5a9a8a8b42"
    EXPECTED_NEWLINE_LF = "sha256:c485220c7f2b51d3960a5e118cf7181debd086140691e1c96db8ec13e1cd84cf"
    EXPECTED_NEWLINE_CRLF = "sha256:5d6a0695821a214ea832b81625179b431f1ff375e1e1492aaa06b138b01b0b9d"
    EXPECTED_KEY_ORDER = "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    EXPECTED_BRANCH_LOWER = "sha256:6461b20cebcb7034bd8b13089d21a90cf5ce300bd7d74eb625c7a342cf6ccdac"
    EXPECTED_BRANCH_UPPER = "sha256:18d62a982fab18f31724a7322a216fe23b797202e99a00ca5005fd417f45ba1d"
    EXPECTED_CONTEXT_GOLDEN = "sha256:78438ac8a3a932631ccedd5c075bfadacf899b7daafac850bc315bad0f88e1d2"

    def test_s01_t04_unicode_preservation_no_normalization(self) -> None:
        """S01-T04: Unicode strings are preserved exactly without normalization (NFC != NFD)."""
        obj_nfc = {"text": "\u00e9"}
        obj_nfd = {"text": "e\u0301"}

        hash_nfc = canonical_hash(obj_nfc)
        hash_nfd = canonical_hash(obj_nfd)

        self.assertEqual(hash_nfc, self.EXPECTED_UNICODE_NFC)
        self.assertEqual(hash_nfd, self.EXPECTED_UNICODE_NFD)
        self.assertNotEqual(hash_nfc, hash_nfd)

    def test_s01_t04_newline_preservation_no_normalization(self) -> None:
        """S01-T04: Newlines are preserved exactly without normalization (LF != CRLF)."""
        obj_lf = {"text": "line1\nline2"}
        obj_crlf = {"text": "line1\r\nline2"}

        hash_lf = canonical_hash(obj_lf)
        hash_crlf = canonical_hash(obj_crlf)

        self.assertEqual(hash_lf, self.EXPECTED_NEWLINE_LF)
        self.assertEqual(hash_crlf, self.EXPECTED_NEWLINE_CRLF)
        self.assertNotEqual(hash_lf, hash_crlf)

    def test_s01_t04_key_order_canonicalization(self) -> None:
        """S01-T04: Key order differences produce identical canonical hashes."""
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}

        hash1 = canonical_hash(obj1)
        hash2 = canonical_hash(obj2)

        self.assertEqual(hash1, self.EXPECTED_KEY_ORDER)
        self.assertEqual(hash2, self.EXPECTED_KEY_ORDER)
        self.assertEqual(hash1, hash2)

    def test_s01_t04_branch_case_sensitivity(self) -> None:
        """S01-T04: Branch names are case-sensitive and produce distinct hashes."""
        obj_lower = {"branch": "main"}
        obj_upper = {"branch": "Main"}

        hash_lower = canonical_hash(obj_lower)
        hash_upper = canonical_hash(obj_upper)

        self.assertEqual(hash_lower, self.EXPECTED_BRANCH_LOWER)
        self.assertEqual(hash_upper, self.EXPECTED_BRANCH_UPPER)
        self.assertNotEqual(hash_lower, hash_upper)

    def test_s01_t04_volatile_observation_metadata_exclusion(self) -> None:
        """S01-T04: Volatile local timestamps and scan IDs are excluded when computing context hash."""
        ctx_with_volatile = {
            "binding": {
                "profile": "default",
                "profile_epoch": 1,
                "repository": "OWNER/REPO",
                "session": "sessions/EXAMPLE",
                "source": "sources/123",
                "starting_branch": "feature/example",
            },
            "state": "waiting",
            "timestamp": "2026-10-07T21:00:00Z",
            "scan_id": "scan-999",
            "scanned_at": "2026-10-07T21:00:01Z",
        }
        ctx_without_volatile = {
            "binding": {
                "profile": "default",
                "profile_epoch": 1,
                "repository": "OWNER/REPO",
                "session": "sessions/EXAMPLE",
                "source": "sources/123",
                "starting_branch": "feature/example",
            },
            "state": "waiting",
        }

        h1 = context_hash(ctx_with_volatile)
        h2 = context_hash(ctx_without_volatile)

        self.assertEqual(h1, self.EXPECTED_CONTEXT_GOLDEN)
        self.assertEqual(h2, self.EXPECTED_CONTEXT_GOLDEN)
        self.assertEqual(h1, h2)


class TestS01T05OperationContractsAndRemoteRepresentability(unittest.TestCase):
    """S01-T05: Every read operation and mutation has a typed contract; unknown fields/states remain representable."""

    def test_s01_t05_fifteen_operations_in_inventory(self) -> None:
        """S01-T05: Fixed inventory contains exactly 15 operations with defined classifications."""
        self.assertEqual(len(OPERATION_INVENTORY), 15)

        expected_ops = {
            "inventory.collect",
            "session.inspect",
            "chats.collect",
            "chats.reply",
            "tasks.create",
            "plans.approve",
            "suggestions.collect",
            "artifacts.export_patch",
            "publication.verify",
            "operations.reconcile",
            "events.read",
            "events.ack",
            "wait",
            "capabilities.inspect",
            "healthcheck",
        }
        self.assertEqual(set(OPERATION_INVENTORY.keys()), expected_ops)

        # Check suggestions API is unsupported_public_api
        self.assertEqual(
            OPERATION_INVENTORY["suggestions.collect"].capability,
            CapabilityClassification.UNSUPPORTED_PUBLIC_API,
        )

        # Check mutations
        mutations = [op for op, spec in OPERATION_INVENTORY.items() if spec.classification == OperationClassification.MUTATION]
        self.assertEqual(set(mutations), {"chats.reply", "tasks.create", "plans.approve"})

    def test_s01_t05_typed_operation_args_and_results(self) -> None:
        """S01-T05: Typed argument and result contract for every operation."""
        # 1. inventory.collect
        inv_args = InventoryCollectArgs(scope="all")
        inv_res = InventoryCollectResult()
        self.assertIsInstance(inv_args, InventoryCollectArgs)
        self.assertIsInstance(inv_res, InventoryCollectResult)

        # 2. session.inspect
        sess_args = SessionInspectArgs(session="sessions/123")
        sess_res = SessionInspectResult(state="ACTIVE")
        self.assertIsInstance(sess_args, SessionInspectArgs)
        self.assertIsInstance(sess_res, SessionInspectResult)

        # 3. chats.collect
        chats_args = ChatsCollectArgs(session="sessions/123")
        chats_res = ChatsCollectResult()
        self.assertIsInstance(chats_args, ChatsCollectArgs)
        self.assertIsInstance(chats_res, ChatsCollectResult)

        # 4. chats.reply
        reply_args = ChatsReplyArgs(session="sessions/123", text="approved text", operation_id="op-1", authorization_ref="ref-1")
        reply_res = ChatsReplyResult(operation_id="op-1", status="ok", delivered=True)
        self.assertIsInstance(reply_args, ChatsReplyArgs)
        self.assertIsInstance(reply_res, ChatsReplyResult)

        # 5. tasks.create
        create_args = TasksCreateArgs(repository="OWNER/REPO", branch="feature/example", title="T", prompt="P", operation_id="op-2", authorization_ref="ref-2")
        create_res = TasksCreateResult(operation_id="op-2")
        self.assertIsInstance(create_args, TasksCreateArgs)
        self.assertIsInstance(create_res, TasksCreateResult)

        # 6. plans.approve
        app_args = PlansApproveArgs(session="sessions/123", plan_id="p-1", operation_id="op-3", authorization_ref="ref-3")
        app_res = PlansApproveResult(session="sessions/123", plan_id="p-1", approved=True)
        self.assertIsInstance(app_args, PlansApproveArgs)
        self.assertIsInstance(app_res, PlansApproveResult)

        # 7. suggestions.collect
        sug_args = SuggestionsCollectArgs(repository="OWNER/REPO")
        sug_res = SuggestionsCollectResult()
        self.assertIsInstance(sug_args, SuggestionsCollectArgs)
        self.assertIsInstance(sug_res, SuggestionsCollectResult)

        # 8. artifacts.export_patch
        art_args = ArtifactsExportPatchArgs(session="sessions/123", destination_dir="/tmp/out")
        art_res = ArtifactsExportPatchResult(exported_path="/tmp/out/p.patch")
        self.assertIsInstance(art_args, ArtifactsExportPatchArgs)
        self.assertIsInstance(art_res, ArtifactsExportPatchResult)

        # 9. publication.verify
        pub_args = PublicationVerifyArgs(repository="OWNER/REPO", branch="feature/example")
        pub_res = PublicationVerifyResult(verified=True, publication_state="published")
        self.assertIsInstance(pub_args, PublicationVerifyArgs)
        self.assertIsInstance(pub_res, PublicationVerifyResult)

        # 10. operations.reconcile
        rec_args = OperationsReconcileArgs(operation_id="op-1")
        rec_res = OperationsReconcileResult(reconciled_state="effect_observed")
        self.assertIsInstance(rec_args, OperationsReconcileArgs)
        self.assertIsInstance(rec_res, OperationsReconcileResult)

        # 11. events.read
        ev_args = EventsReadArgs(limit=10)
        ev_res = EventsReadResult()
        self.assertIsInstance(ev_args, EventsReadArgs)
        self.assertIsInstance(ev_res, EventsReadResult)

        # 12. events.ack
        ack_args = EventsAckArgs(event_ids=("ev-1",))
        ack_res = EventsAckResult(acked_event_ids=("ev-1",), success=True)
        self.assertIsInstance(ack_args, EventsAckArgs)
        self.assertIsInstance(ack_res, EventsAckResult)

        # 13. wait
        wait_args = WaitArgs(predicate="all_terminal", timeout_seconds=10.0)
        wait_res = WaitResult(resumed=True, predicate_matched=True)
        self.assertIsInstance(wait_args, WaitArgs)
        self.assertIsInstance(wait_res, WaitResult)

        # 14. capabilities.inspect
        cap_args = CapabilitiesInspectArgs(profile="default")
        cap_res = CapabilitiesInspectResult()
        self.assertIsInstance(cap_args, CapabilitiesInspectArgs)
        self.assertIsInstance(cap_res, CapabilitiesInspectResult)

        # 15. healthcheck
        hc_args = HealthcheckArgs(profile="default")
        hc_res = HealthcheckResult(healthy=True)
        self.assertIsInstance(hc_args, HealthcheckArgs)
        self.assertIsInstance(hc_res, HealthcheckResult)

    def test_s01_t05_remote_records_tolerate_unknown_fields_and_states(self) -> None:
        """S01-T05: Remote response records tolerate unknown fields and preserve unfamiliar state strings verbatim."""
        # 1. SessionRecord preserves unknown fields and unexpected state string
        raw_session = {
            "name": "sessions/EXAMPLE",
            "state": "FUTURE_UNRECOGNIZED_STATE",
            "id": "12345",
            "title": "Example Session",
            "createTime": "2026-10-07T00:00:00Z",
            "updateTime": "2026-10-07T01:00:00Z",
            "requirePlanApproval": True,
            "sourceContext": {"githubRepo": {"owner": "OWNER", "repo": "REPO"}},
            "futureFieldFoo": "bar",
            "extraCounter": 42,
        }
        session_rec = SessionRecord.from_dict(raw_session)
        self.assertEqual(session_rec.name, "sessions/EXAMPLE")
        self.assertEqual(session_rec.state, "FUTURE_UNRECOGNIZED_STATE")
        unknown_dict = dict(session_rec.unknown_fields)
        self.assertEqual(unknown_dict.get("futureFieldFoo"), "bar")
        self.assertEqual(unknown_dict.get("extraCounter"), 42)

        # 2. SourceRecord preserves unknown fields
        raw_source = {
            "name": "sources/SRC1",
            "id": "src-1",
            "githubRepo": {"owner": "OWNER", "repo": "REPO"},
            "undocumentedVendorMeta": {"zone": "us-east"},
        }
        source_rec = SourceRecord.from_dict(raw_source)
        self.assertEqual(source_rec.name, "sources/SRC1")
        self.assertEqual(source_rec.github_repo_owner, "OWNER")
        self.assertEqual(source_rec.github_repo_name, "REPO")
        unknown_src = dict(source_rec.unknown_fields)
        self.assertEqual(unknown_src.get("undocumentedVendorMeta"), {"zone": "us-east"})

        # 3. ActivityRecord preserves unknown fields and unknown activity type
        raw_activity = {
            "name": "sessions/EXAMPLE/activities/ACT1",
            "type": "NEW_UNEXPECTED_ACTIVITY_KIND",
            "id": "act-1",
            "createTime": "2026-10-07T02:00:00Z",
            "customAnnotation": "testing",
        }
        activity_rec = ActivityRecord.from_dict(raw_activity)
        self.assertEqual(activity_rec.name, "sessions/EXAMPLE/activities/ACT1")
        self.assertEqual(activity_rec.activity_type, "NEW_UNEXPECTED_ACTIVITY_KIND")
        unknown_act = dict(activity_rec.unknown_fields)
        self.assertEqual(unknown_act.get("customAnnotation"), "testing")

    def test_s01_t05_operation_state_transitions(self) -> None:
        """S01-T05: OperationState transition table enforces legal progression."""
        # Legal transitions
        self.assertTrue(is_legal_operation_transition(OperationState.PREPARED, OperationState.DISPATCHING))
        self.assertTrue(is_legal_operation_transition(OperationState.PREPARED, OperationState.BLOCKED_BEFORE_DISPATCH))
        self.assertTrue(is_legal_operation_transition(OperationState.PREPARED, OperationState.CANCELLED_BEFORE_DISPATCH))
        self.assertTrue(is_legal_operation_transition(OperationState.DISPATCHING, OperationState.ACCEPTED))
        self.assertTrue(is_legal_operation_transition(OperationState.DISPATCHING, OperationState.REJECTED))
        self.assertTrue(is_legal_operation_transition(OperationState.DISPATCHING, OperationState.UNKNOWN))
        self.assertTrue(is_legal_operation_transition(OperationState.ACCEPTED, OperationState.EFFECT_OBSERVED))
        self.assertTrue(is_legal_operation_transition(OperationState.UNKNOWN, OperationState.EFFECT_OBSERVED))

        # Illegal transitions
        self.assertFalse(is_legal_operation_transition(OperationState.PREPARED, OperationState.EFFECT_OBSERVED))
        self.assertFalse(is_legal_operation_transition(OperationState.EFFECT_OBSERVED, OperationState.DISPATCHING))
        self.assertFalse(is_legal_operation_transition(OperationState.REJECTED, OperationState.ACCEPTED))
        self.assertFalse(is_legal_operation_transition(OperationState.BLOCKED_BEFORE_DISPATCH, OperationState.DISPATCHING))

    def test_s01_t05_exit_code_precedence(self) -> None:
        """S01-T05: Exit code precedence combines correctly: 130 > 4 > 3 > 5 > 2 > 0."""
        self.assertEqual(combine_exit_codes([0, 2, 5, 3, 4, 130]), EXIT_INTERRUPTED)
        self.assertEqual(combine_exit_codes([0, 2, 5, 3, 4]), EXIT_MUTATION_BLOCKED)
        self.assertEqual(combine_exit_codes([0, 2, 5, 3]), EXIT_FATAL_READ_OR_LOCAL)
        self.assertEqual(combine_exit_codes([0, 2, 5]), EXIT_PARTIAL_OR_UNSUPPORTED)
        self.assertEqual(combine_exit_codes([0, 2]), EXIT_WAITING)
        self.assertEqual(combine_exit_codes([0]), EXIT_OK)
        self.assertEqual(combine_exit_codes([]), EXIT_OK)

    def test_s01_t05_result_builder_builds_and_validates(self) -> None:
        """S01-T05: ResultBuilder creates a valid result with derived exit code."""
        builder = ResultBuilder(plan_id="test-plan-01")
        builder.add_action_result(
            ActionResult.create(
                action_id="act-1",
                op="inventory.collect",
                status=ActionResultStatus.OK,
                exit_code=0,
                data={"sessions": []},
            )
        )
        builder.add_action_result(
            ActionResult.create(
                action_id="act-2",
                op="wait",
                status=ActionResultStatus.WAITING,
                exit_code=2,
            )
        )
        res = builder.build()
        validate_result(res)
        self.assertEqual(res["exit_code"], EXIT_WAITING)
        self.assertEqual(res["status"], "waiting")


class InMemoryTicketAuthority:
    """Reference in-memory TicketAuthority stub for testing single-use redemption."""

    def __init__(self) -> None:
        self.minted_tickets: dict[str, tuple[str, str, str]] = {}
        self.consumed_tickets: set[str] = set()

    def mint(self, operation_id: str, request_hash: str, nonce: str = "nonce-123") -> DispatchTicket:
        import uuid
        ticket_id = f"ticket-{uuid.uuid4().hex[:8]}"
        self.minted_tickets[ticket_id] = (operation_id, request_hash, nonce)
        return DispatchTicket(
            ticket_id=ticket_id,
            operation_id=operation_id,
            request_hash=request_hash,
            nonce=nonce,
        )

    def redeem(self, ticket: DispatchTicket, request_hash: str) -> bool:
        if ticket.ticket_id in self.consumed_tickets:
            return False
        expected = self.minted_tickets.get(ticket.ticket_id)
        if expected is None:
            return False
        exp_op_id, exp_req_hash, exp_nonce = expected
        if (
            ticket.operation_id != exp_op_id
            or ticket.request_hash != exp_req_hash
            or ticket.nonce != exp_nonce
            or request_hash != exp_req_hash
        ):
            return False
        self.consumed_tickets.add(ticket.ticket_id)
        return True


class TestS01T05TicketAuthorityAndMutationResponse(unittest.TestCase):
    """S01-T05 / F3 & F4: TicketAuthority redemption semantics and MutationResponse contract."""

    def test_hand_constructed_ticket_not_redeemable(self) -> None:
        """F3: A hand-constructed ticket not minted by the authority cannot be redeemed."""
        authority = InMemoryTicketAuthority()
        fake_ticket = DispatchTicket(
            ticket_id="ticket-forged",
            operation_id="op-123",
            request_hash="sha256:abc",
            nonce="fake-nonce",
        )
        self.assertFalse(authority.redeem(fake_ticket, "sha256:abc"))

    def test_minted_ticket_redeemable_once_and_second_redeem_fails(self) -> None:
        """F3: A journal-minted ticket is redeemable exactly once; second redeem returns False."""
        authority = InMemoryTicketAuthority()
        req_hash = "sha256:real-hash-123"
        ticket = authority.mint(operation_id="op-valid", request_hash=req_hash, nonce="secure-nonce")

        # First redeem succeeds
        self.assertTrue(authority.redeem(ticket, req_hash))

        # Second redeem with identical ticket fails (single use)
        self.assertFalse(authority.redeem(ticket, req_hash))

    def test_redeem_with_mismatched_request_hash_fails(self) -> None:
        """F3: Redeeming with a mismatched request_hash fails and does not consume ticket."""
        authority = InMemoryTicketAuthority()
        req_hash = "sha256:real-hash-123"
        ticket = authority.mint(operation_id="op-valid", request_hash=req_hash, nonce="secure-nonce")

        # Mismatched request hash fails
        self.assertFalse(authority.redeem(ticket, "sha256:wrong-hash"))

        # Ticket can still be redeemed with correct hash
        self.assertTrue(authority.redeem(ticket, req_hash))

    def test_mutation_response_contract_and_uncertain_effect(self) -> None:
        """F4: MutationResponse carries outcome and optional session, preserving uncertain_effect."""
        outcome_timeout = TransportOutcome(
            status=0,
            body=None,
            request_count=1,
            byte_count=0,
            uncertain_effect=True,
            sanitized_error_code=ErrorCode.TIMEOUT,
        )
        resp_timeout = MutationResponse(outcome=outcome_timeout, session=None)
        self.assertTrue(resp_timeout.outcome.uncertain_effect)
        self.assertIsNone(resp_timeout.session)

        outcome_rejected = TransportOutcome(
            status=400,
            body=b"bad request",
            request_count=1,
            byte_count=11,
            uncertain_effect=False,
            sanitized_error_code=ErrorCode.INVALID_INPUT,
        )
        resp_rejected = MutationResponse(outcome=outcome_rejected, session=None)
        self.assertFalse(resp_rejected.outcome.uncertain_effect)

        outcome_ok = TransportOutcome(
            status=200,
            body=b"{}",
            request_count=1,
            byte_count=2,
            uncertain_effect=False,
        )
        sess = SessionRecord(name="sessions/123", state="ACTIVE")
        resp_ok = MutationResponse(outcome=outcome_ok, session=sess)
        self.assertEqual(resp_ok.session.name, "sessions/123")


if __name__ == "__main__":
    unittest.main()

