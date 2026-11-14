"""Tests for the policy engine's lookup logic.

These use a small test table, so they keep passing when the real table's
colors change. The real table is tested in test_policy_table.py.
"""

from datetime import datetime, timezone

import pytest

from fieldnote.audit import AuditLog
from fieldnote.policy import ActionRequest, PolicyEngine, Rule, Tier, validate_table

TEST_TABLE = {
    "g.act": Rule(Tier.GREEN),
    "y.act": Rule(Tier.YELLOW),
    "r.act": Rule(Tier.RED, approver="boss"),
}


@pytest.fixture
def audit(tmp_path):
    clock = lambda: datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    with AuditLog(tmp_path / "audit.db", clock=clock) as log:
        yield log


@pytest.fixture
def engine(audit):
    return PolicyEngine(audit, TEST_TABLE)


# --- Unknown action types ----------------------------------------------------


@pytest.mark.parametrize(
    "action_type",
    ["not.in.table", "G.ACT", " g.act", "g.act ", "quote.send", "invoice.post"],
)
def test_action_type_not_in_table_is_refused(engine, action_type):
    decision = engine.check(ActionRequest(action_type, requester="rep"))
    assert not decision.allowed
    assert decision.tier is None
    assert decision.reason == "action type not in policy table"


# --- Green -------------------------------------------------------------------


def test_green_is_allowed(engine):
    decision = engine.check(ActionRequest("g.act", requester="rep"))
    assert decision.allowed
    assert decision.tier is Tier.GREEN


# --- Yellow ------------------------------------------------------------------


def test_yellow_confirmed_by_record_owner_is_allowed(engine):
    request = ActionRequest("y.act", requester="rep", record_owner="rep", approved_by="rep")
    decision = engine.check(request)
    assert decision.allowed
    assert decision.tier is Tier.YELLOW


def test_yellow_without_confirmation_is_refused(engine):
    decision = engine.check(ActionRequest("y.act", requester="rep", record_owner="rep"))
    assert not decision.allowed
    assert decision.reason == "needs confirmation by the record owner"


def test_yellow_confirmed_by_someone_else_is_refused(engine):
    request = ActionRequest("y.act", requester="rep", record_owner="rep", approved_by="other")
    decision = engine.check(request)
    assert not decision.allowed
    assert decision.reason == "confirmed by someone other than the record owner"


def test_yellow_with_unknown_record_owner_is_refused(engine):
    decision = engine.check(ActionRequest("y.act", requester="rep", approved_by="rep"))
    assert not decision.allowed
    assert decision.reason == "record owner unknown"


# --- Red ---------------------------------------------------------------------


def test_red_approved_by_named_approver_is_allowed(engine):
    decision = engine.check(ActionRequest("r.act", requester="rep", approved_by="boss"))
    assert decision.allowed
    assert decision.tier is Tier.RED


def test_red_without_approval_is_refused(engine):
    decision = engine.check(ActionRequest("r.act", requester="rep"))
    assert not decision.allowed
    assert decision.reason == "needs approval by boss"


def test_red_self_approval_is_refused(engine):
    decision = engine.check(ActionRequest("r.act", requester="rep", approved_by="rep"))
    assert not decision.allowed
    assert decision.reason == "self-approval is not allowed"


def test_red_named_approver_cannot_approve_own_request(engine):
    # Even the named approver cannot approve something they requested.
    decision = engine.check(ActionRequest("r.act", requester="boss", approved_by="boss"))
    assert not decision.allowed
    assert decision.reason == "self-approval is not allowed"


def test_red_approved_by_wrong_person_is_refused(engine):
    decision = engine.check(ActionRequest("r.act", requester="rep", approved_by="other"))
    assert not decision.allowed
    assert decision.reason == "approved by someone other than boss"


# --- Classification never comes from the request ------------------------------


def test_tier_claimed_in_details_is_ignored(engine):
    # A model or a note could put this text in the details. It must not matter.
    request = ActionRequest("r.act", requester="rep", details={"tier": "green"})
    decision = engine.check(request)
    assert not decision.allowed
    assert decision.tier is Tier.RED


def test_request_has_no_tier_field():
    with pytest.raises(TypeError):
        ActionRequest("r.act", requester="rep", tier=Tier.GREEN)


def test_engine_keeps_its_own_copy_of_the_table(audit):
    table = dict(TEST_TABLE)
    engine = PolicyEngine(audit, table)
    table["r.act"] = Rule(Tier.GREEN)  # changed after the engine was built

    decision = engine.check(ActionRequest("r.act", requester="rep"))
    assert not decision.allowed
    assert decision.tier is Tier.RED


# --- Every decision is logged ------------------------------------------------


def test_every_decision_is_written_to_audit_log(engine, audit):
    engine.check(ActionRequest("g.act", requester="rep"))
    engine.check(ActionRequest("not.in.table", requester="rep"))
    engine.check(ActionRequest("r.act", requester="rep", approved_by="rep"))

    events = audit.events()
    assert [(e.action_type, e.outcome) for e in events] == [
        ("g.act", "allowed"),
        ("not.in.table", "refused"),
        ("r.act", "refused"),
    ]
    self_approval = events[2]
    assert self_approval.actor == "rep"
    assert self_approval.details["tier"] == "red"
    assert self_approval.details["reason"] == "self-approval is not allowed"
    assert self_approval.details["approved_by"] == "rep"


# --- Malformed rules and requests ----------------------------------------------


def test_red_rule_without_approver_is_rejected():
    with pytest.raises(ValueError, match="approver"):
        Rule(Tier.RED)


@pytest.mark.parametrize("tier", [Tier.GREEN, Tier.YELLOW])
def test_non_red_rule_with_approver_is_rejected(tier):
    with pytest.raises(ValueError, match="only red"):
        Rule(tier, approver="boss")


def test_rule_with_plain_string_tier_is_rejected():
    with pytest.raises(ValueError, match="tier"):
        Rule("green")


@pytest.mark.parametrize("bad_table", [{"": Rule(Tier.GREEN)}, {"x.y": "green"}])
def test_malformed_table_is_rejected(bad_table):
    with pytest.raises(ValueError):
        validate_table(bad_table)


@pytest.mark.parametrize("field", ["action_type", "requester"])
def test_blank_action_type_or_requester_is_rejected(field):
    args = {"action_type": "g.act", "requester": "rep"}
    args[field] = "  "
    with pytest.raises(ValueError):
        ActionRequest(**args)
