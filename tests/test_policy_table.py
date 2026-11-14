"""Tests for the real policy table (the developer's decisions).

If a color is changed in policy_table.py, test_table_matches_approved_colors
fails on purpose: the change has to be made deliberately in both places.
"""

import re
from datetime import datetime, timezone

import pytest

from fieldnote.audit import AuditLog
from fieldnote.policy import ActionRequest, PolicyEngine, Rule, Tier
from fieldnote.policy_table import POLICY_TABLE

APPROVED = {
    "note.submit": (Tier.GREEN, None),
    "extraction.run": (Tier.GREEN, None),
    "candidate.edit": (Tier.GREEN, None),
    "candidate.reject": (Tier.GREEN, None),
    "candidate.confirm": (Tier.GREEN, None),
    "crm.upsert": (Tier.YELLOW, None),
    "quote.draft": (Tier.GREEN, None),
    "quote.validate": (Tier.GREEN, None),
    "quote.release": (Tier.YELLOW, None),
    "quote.release_discounted": (Tier.RED, "owner"),
    "audit.export": (Tier.GREEN, None),
}


@pytest.fixture
def engine(tmp_path):
    clock = lambda: datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
    with AuditLog(tmp_path / "audit.db", clock=clock) as log:
        yield PolicyEngine(log, POLICY_TABLE)


def test_table_matches_approved_colors():
    actual = {name: (rule.tier, rule.approver) for name, rule in POLICY_TABLE.items()}
    assert actual == APPROVED


def test_table_cannot_be_changed_at_runtime():
    with pytest.raises(TypeError):
        POLICY_TABLE["quote.release_discounted"] = Rule(Tier.GREEN)


def test_no_action_type_sends_to_customer_or_posts_invoice():
    forbidden = {"send", "email", "invoice", "post", "notify"}
    for action_type in POLICY_TABLE:
        words = set(re.split(r"[._]", action_type))
        assert not words & forbidden, action_type


def test_salesperson_cannot_approve_own_discount(engine):
    request = ActionRequest(
        "quote.release_discounted", requester="sales_rep", approved_by="sales_rep"
    )
    decision = engine.check(request)
    assert not decision.allowed
    assert decision.reason == "self-approval is not allowed"


def test_owner_can_approve_salespersons_discount(engine):
    request = ActionRequest(
        "quote.release_discounted", requester="sales_rep", approved_by="owner"
    )
    assert engine.check(request).allowed


def test_crm_write_needs_salesperson_confirmation(engine):
    unconfirmed = ActionRequest("crm.upsert", requester="sales_rep", record_owner="sales_rep")
    confirmed = ActionRequest(
        "crm.upsert", requester="sales_rep", record_owner="sales_rep", approved_by="sales_rep"
    )
    assert not engine.check(unconfirmed).allowed
    assert engine.check(confirmed).allowed
