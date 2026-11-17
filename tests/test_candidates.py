"""Tests for the candidate store."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from fieldnote.audit import AuditLog
from fieldnote.candidates import MAX_VALUE_CHARS, CandidateStore, NoteLocked
from fieldnote.extraction import MAX_NOTE_CHARS, check_model_output
from fieldnote.policy import ActionRefused, PolicyEngine, Rule, Tier
from fieldnote.policy_table import POLICY_TABLE

PRODUCTS = {
    "ARM-6KG": "Collaborative arm 6-axis 6 kg payload",
    "GRIP-2F-85": "Two-finger electric gripper 85 mm stroke",
}

NOTE = (
    "Met Dana at Bayside Packaging. They want two 6 kg arms and a gripper. "
    "I told her we could do 5% off if they sign this month. Call her Friday."
)


def _field(text):
    return {"value": text, "quote": text, "kind": "observed"}


ANSWER = json.dumps({
    "account": {"name": _field("Bayside Packaging")},
    "contact": {"name": _field("Dana"), "email": None, "phone": None},
    "items": [
        {"product_id": "ARM-6KG", "quantity": 2, "quote": "two 6 kg arms", "kind": "customer_stated"},
        {"product_id": None, "quantity": 1, "quote": "a gripper", "kind": "customer_stated"},
    ],
    "commitments": [{"quote": "5% off if they sign this month"}],
    "task": {"subject": _field("Call her Friday"), "due": _field("Friday")},
})
EXTRACTION = check_model_output(NOTE, ANSWER, PRODUCTS)

CLOCK = lambda: datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"  # the audit log and the store share one file


@pytest.fixture
def audit(db_path):
    with AuditLog(db_path, clock=CLOCK) as log:
        yield log


def make_store(db_path, audit, table=POLICY_TABLE):
    return CandidateStore(
        db_path, policy=PolicyEngine(audit, table), audit=audit, product_names=PRODUCTS, clock=CLOCK
    )


@pytest.fixture
def store(db_path, audit):
    with make_store(db_path, audit) as s:
        yield s


@pytest.fixture
def note(store):
    return store.submit_note(NOTE, "sales_rep")


@pytest.fixture
def saved(store, note):
    """The candidates of the worked example, by a short name."""
    by_position = store.save_extraction(note.id, EXTRACTION)
    names = ["account", "contact", "email", "phone", "subject", "due", "arms", "gripper", "promise"]
    return dict(zip(names, by_position, strict=True))


def raw(db_path):
    return sqlite3.connect(db_path, isolation_level=None)


def log_of(audit):
    return [(e.actor, e.action_type, e.outcome) for e in audit.events()]


# --- Notes -----------------------------------------------------------------------


def test_note_is_saved_and_logged(store, audit):
    note = store.submit_note(NOTE, "sales_rep")
    assert (note.author, note.text, note.locked) == ("sales_rep", NOTE, False)
    assert note.created_at == "2026-11-16T15:00:00+00:00"
    assert store.get_note(note.id) == note
    assert log_of(audit) == [
        ("sales_rep", "note.submit", "allowed"),
        ("sales_rep", "note.submit", "succeeded"),
    ]
    assert audit.events()[1].details == {"note_id": note.id}


@pytest.mark.parametrize("text", ["", "  \n", None, "x" * (MAX_NOTE_CHARS + 1)])
def test_blank_or_oversized_note_is_refused(store, audit, text):
    with pytest.raises(ValueError):
        store.submit_note(text, "sales_rep")
    assert audit.events() == []


@pytest.mark.parametrize(
    "sql", ["UPDATE notes SET text = 'changed'", "DELETE FROM notes"]
)
def test_saved_note_cannot_be_changed_even_with_raw_sql(store, note, db_path, sql):
    conn = raw(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="cannot be changed"):
        conn.execute(sql)
    conn.close()
    assert store.get_note(note.id).text == NOTE


def test_unknown_note_or_candidate_is_an_error(store):
    with pytest.raises(LookupError):
        store.get_note(999)
    with pytest.raises(LookupError):
        store.get(999)
    with pytest.raises(LookupError):
        store.confirm(999, "sales_rep")
    with pytest.raises(LookupError):
        store.save_extraction(999, EXTRACTION)


# --- Saving candidates ---------------------------------------------------------------


def test_candidates_come_back_as_extracted(saved):
    assert [(c.type, c.name, c.state) for c in saved.values()] == [
        ("field", "account.name", "pending"),
        ("field", "contact.name", "pending"),
        ("field", "contact.email", "not_stated"),
        ("field", "contact.phone", "not_stated"),
        ("field", "task.subject", "pending"),
        ("field", "task.due", "pending"),
        ("item", "item", "pending"),
        ("item", "item", "pending"),
        ("commitment", "commitment", "pending"),
    ]
    account = saved["account"]
    assert (account.value, account.kind, account.edited) == ("Bayside Packaging", "observed", False)
    assert NOTE[account.span[0] : account.span[1]] == "Bayside Packaging"
    assert not account.needs_individual_confirmation

    arms = saved["arms"]
    assert (arms.product_id, arms.quantity, arms.value) == ("ARM-6KG", 2, None)
    assert arms.original_confidence == "low" and len(arms.original_reasons) == 2
    assert arms.needs_individual_confirmation

    assert saved["gripper"].product_id is None
    assert saved["promise"].value == "5% off if they sign this month"
    assert saved["promise"].needs_individual_confirmation
    assert saved["email"].value is None and saved["email"].span is None


def test_candidates_can_only_be_saved_once_per_note(store, note, saved):
    with pytest.raises(ValueError, match="already saved"):
        store.save_extraction(note.id, EXTRACTION)
    assert len(store.candidates(note.id)) == 9


def test_candidates_survive_closing_and_reopening(db_path, audit):
    with make_store(db_path, audit) as first:
        note = first.submit_note(NOTE, "sales_rep")
        before = first.save_extraction(note.id, EXTRACTION)
    with make_store(db_path, audit) as reopened:
        assert reopened.candidates(note.id) == before


# --- Confirm and reject ----------------------------------------------------------------


def test_confirm_sets_state_and_is_logged(store, audit, saved):
    done = store.confirm(saved["account"].id, "sales_rep")
    assert (done.state, done.decided_by) == ("confirmed", "sales_rep")
    assert done.decided_at == "2026-11-16T15:00:00+00:00"

    last = audit.events()[-1]
    assert (last.actor, last.action_type, last.outcome) == ("sales_rep", "candidate.confirm", "allowed")
    assert last.details["request_details"] == {
        "note_id": saved["account"].note_id,
        "candidate_id": saved["account"].id,
        "name": "account.name",
        "bulk": False,
    }
    assert last.details["record_owner"] == "sales_rep"


def test_reject_sets_state_and_is_logged(store, audit, saved):
    done = store.reject(saved["contact"].id, "sales_rep")
    assert (done.state, done.decided_by) == ("rejected", "sales_rep")
    assert log_of(audit)[-1] == ("sales_rep", "candidate.reject", "allowed")


def test_second_tap_changes_nothing_and_is_not_logged_again(store, audit, saved):
    store.confirm(saved["account"].id, "sales_rep")
    store.reject(saved["contact"].id, "sales_rep")
    entries = len(audit.events())
    assert store.confirm(saved["account"].id, "sales_rep").state == "confirmed"
    assert store.reject(saved["contact"].id, "sales_rep").state == "rejected"
    assert len(audit.events()) == entries


def test_reviewer_can_change_his_mind(store, saved):
    account = saved["account"].id
    assert store.reject(account, "sales_rep").state == "rejected"
    assert store.confirm(account, "sales_rep").state == "confirmed"
    assert store.reject(account, "sales_rep").state == "rejected"


def test_field_the_note_does_not_state_cannot_be_confirmed_or_rejected(store, saved):
    with pytest.raises(ValueError, match="does not state"):
        store.confirm(saved["email"].id, "sales_rep")
    with pytest.raises(ValueError, match="does not state"):
        store.reject(saved["email"].id, "sales_rep")
    assert store.get(saved["email"].id).state == "not_stated"


def test_item_without_a_product_cannot_be_confirmed(store, saved):
    with pytest.raises(ValueError, match="pick a product"):
        store.confirm(saved["gripper"].id, "sales_rep")
    assert store.get(saved["gripper"].id).state == "pending"


@pytest.mark.parametrize("actor", ["", "  ", None])
def test_blank_actor_is_refused(store, saved, actor):
    with pytest.raises(ValueError):
        store.confirm(saved["account"].id, actor)
    assert store.get(saved["account"].id).state == "pending"


# --- Confirm all -------------------------------------------------------------------------


def test_confirm_all_confirms_plain_ones_and_returns_the_rest(store, audit, saved):
    result = store.confirm_all(saved["account"].note_id, "sales_rep")

    plain = [saved[k].id for k in ("account", "contact", "subject", "due")]
    assert list(result.confirmed) == plain
    assert [c.id for c in result.waiting] == [saved[k].id for k in ("arms", "gripper", "promise")]

    for key in ("account", "contact", "subject", "due"):
        assert store.get(saved[key].id).state == "confirmed"
    for key in ("arms", "gripper", "promise"):
        assert store.get(saved[key].id).state == "pending"
    for key in ("email", "phone"):
        assert store.get(saved[key].id).state == "not_stated"

    bulk_entries = [e for e in audit.events() if e.action_type == "candidate.confirm"]
    assert len(bulk_entries) == 4
    assert all(e.details["request_details"]["bulk"] is True for e in bulk_entries)


def test_confirm_all_twice_confirms_nothing_new(store, saved):
    note_id = saved["account"].note_id
    store.confirm_all(note_id, "sales_rep")
    again = store.confirm_all(note_id, "sales_rep")
    assert again.confirmed == ()
    assert len(again.waiting) == 3


def test_confirm_all_leaves_rejected_ones_rejected(store, saved):
    store.reject(saved["due"].id, "sales_rep")
    store.confirm_all(saved["due"].note_id, "sales_rep")
    assert store.get(saved["due"].id).state == "rejected"


def test_flagged_candidates_are_confirmed_one_at_a_time(store, saved):
    assert store.confirm(saved["arms"].id, "sales_rep").state == "confirmed"
    assert store.confirm(saved["promise"].id, "sales_rep").state == "confirmed"


# --- Edits: an edit never confirms ---------------------------------------------------------


def test_edit_keeps_the_original_and_goes_back_to_pending(store, audit, saved):
    account = saved["account"].id
    store.confirm(account, "sales_rep")

    edited = store.edit_value(account, "sales_rep", "Bayside Packaging LLC")
    assert edited.value == "Bayside Packaging LLC"
    assert edited.original_value == "Bayside Packaging"
    assert edited.edited is True
    assert edited.span is None and edited.original_span == saved["account"].original_span
    assert (edited.state, edited.decided_by, edited.decided_at) == ("pending", None, None)
    assert edited.needs_individual_confirmation

    last = audit.events()[-1]
    assert (last.action_type, last.outcome) == ("candidate.edit", "allowed")
    assert last.details["request_details"]["from"] == "Bayside Packaging"
    assert last.details["request_details"]["to"] == "Bayside Packaging LLC"


def test_edited_field_needs_its_own_confirm_tap(store, saved):
    account = saved["account"].id
    store.edit_value(account, "sales_rep", "Bayside Packaging LLC")

    result = store.confirm_all(saved["account"].note_id, "sales_rep")
    assert account not in result.confirmed
    assert account in [c.id for c in result.waiting]
    assert store.get(account).state == "pending"

    assert store.confirm(account, "sales_rep").state == "confirmed"


def test_field_the_note_did_not_state_can_be_filled_in(store, saved):
    filled = store.edit_value(saved["email"].id, "sales_rep", "dana@bayside.example")
    assert (filled.value, filled.state, filled.edited, filled.kind) == (
        "dana@bayside.example", "pending", True, "observed",
    )
    assert filled.original_value is None and filled.span is None
    assert store.confirm(filled.id, "sales_rep").state == "confirmed"


def test_editing_a_commitment_keeps_it_a_commitment(store, saved):
    edited = store.edit_value(saved["promise"].id, "sales_rep", "5% off if signed by Nov 30")
    assert edited.kind == "commitment" and edited.state == "pending"


def test_edit_to_the_same_value_changes_nothing(store, audit, saved):
    entries = len(audit.events())
    same = store.edit_value(saved["account"].id, "sales_rep", "Bayside Packaging")
    assert same.edited is False
    assert len(audit.events()) == entries


@pytest.mark.parametrize("value", ["", "   ", None, 5, "x" * (MAX_VALUE_CHARS + 1)])
def test_bad_new_value_is_refused(store, saved, value):
    with pytest.raises(ValueError):
        store.edit_value(saved["account"].id, "sales_rep", value)
    assert store.get(saved["account"].id) == saved["account"]


def test_picking_a_product_then_confirming_an_item(store, saved):
    gripper = saved["gripper"].id
    picked = store.edit_item(gripper, "sales_rep", product_id="GRIP-2F-85")
    assert (picked.product_id, picked.quantity, picked.state, picked.edited) == (
        "GRIP-2F-85", 1, "pending", True,
    )
    assert picked.original_product_id is None
    assert store.confirm(gripper, "sales_rep").state == "confirmed"


def test_changing_only_the_quantity_keeps_the_product(store, saved):
    changed = store.edit_item(saved["arms"].id, "sales_rep", quantity=3)
    assert (changed.product_id, changed.quantity) == ("ARM-6KG", 3)
    assert (changed.original_product_id, changed.original_quantity) == ("ARM-6KG", 2)


@pytest.mark.parametrize("product_id", ["ARM-99KG", "arm-6kg", "", 7])
def test_item_cannot_be_edited_to_a_product_that_is_not_in_the_catalog(store, saved, product_id):
    with pytest.raises(ValueError, match="catalog"):
        store.edit_item(saved["arms"].id, "sales_rep", product_id=product_id)
    assert store.get(saved["arms"].id) == saved["arms"]


@pytest.mark.parametrize("quantity", [0, -1, 2.5, "3", True, 10**9])
def test_item_cannot_be_edited_to_a_bad_quantity(store, saved, quantity):
    with pytest.raises(ValueError, match="quantity"):
        store.edit_item(saved["arms"].id, "sales_rep", quantity=quantity)
    assert store.get(saved["arms"].id) == saved["arms"]


def test_each_kind_of_edit_only_works_on_its_own_type(store, saved):
    with pytest.raises(ValueError):
        store.edit_value(saved["arms"].id, "sales_rep", "three arms")
    with pytest.raises(ValueError):
        store.edit_item(saved["account"].id, "sales_rep", quantity=2)
    with pytest.raises(ValueError):
        store.edit_item(saved["arms"].id, "sales_rep")


# --- Database-level protection ----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE candidates SET original_value = 'Acme' WHERE name = 'account.name'",
        "UPDATE candidates SET confidence = 'high' WHERE type = 'item'",
        "UPDATE candidates SET span_start = 0 WHERE name = 'account.name'",
        "DELETE FROM candidates WHERE type = 'commitment'",
    ],
)
def test_what_the_ai_suggested_cannot_be_changed_with_raw_sql(store, saved, db_path, sql):
    conn = raw(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
    conn.close()
    assert store.candidates(saved["account"].note_id) == tuple(saved.values())


# --- Locking -------------------------------------------------------------------------------------


def test_nothing_changes_after_the_note_is_locked(store, saved, db_path):
    note_id = saved["account"].note_id
    store.confirm(saved["account"].id, "sales_rep")
    store.lock_note(note_id, "sales_rep")
    assert store.get_note(note_id).locked
    before = store.candidates(note_id)

    with pytest.raises(NoteLocked):
        store.confirm(saved["contact"].id, "sales_rep")
    with pytest.raises(NoteLocked):
        store.reject(saved["account"].id, "sales_rep")
    with pytest.raises(NoteLocked):
        store.edit_value(saved["account"].id, "sales_rep", "Acme")
    with pytest.raises(NoteLocked):
        store.edit_item(saved["arms"].id, "sales_rep", quantity=5)
    with pytest.raises(NoteLocked):
        store.confirm_all(note_id, "sales_rep")

    conn = raw(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        conn.execute("UPDATE candidates SET state = 'confirmed' WHERE note_id = ?", (note_id,))
    with pytest.raises(sqlite3.IntegrityError, match="lock cannot be changed"):
        conn.execute("DELETE FROM note_locks")
    conn.close()

    assert store.candidates(note_id) == before


def test_locking_twice_is_harmless_and_locks_only_that_note(store, saved):
    other = store.submit_note("Second visit, nothing new.", "sales_rep")
    store.lock_note(saved["account"].note_id, "sales_rep")
    store.lock_note(saved["account"].note_id, "owner")
    assert store.get_note(saved["account"].note_id).locked
    assert not store.get_note(other.id).locked


def test_locked_note_does_not_stop_work_on_another_note(store, saved):
    other = store.submit_note(NOTE, "sales_rep")
    others = store.save_extraction(other.id, EXTRACTION)
    store.lock_note(saved["account"].note_id, "sales_rep")
    assert store.confirm(others[0].id, "sales_rep").state == "confirmed"


# --- Policy -----------------------------------------------------------------------------------------


def test_a_policy_refusal_changes_nothing(db_path, audit):
    # A table with no candidate.confirm: that action type is unknown, so refused.
    table = {"note.submit": Rule(Tier.GREEN), "candidate.edit": Rule(Tier.GREEN)}
    with make_store(db_path, audit, table) as strict:
        note = strict.submit_note(NOTE, "sales_rep")
        account = strict.save_extraction(note.id, EXTRACTION)[0]

        with pytest.raises(ActionRefused):
            strict.confirm(account.id, "sales_rep")
        with pytest.raises(ActionRefused):
            strict.reject(account.id, "sales_rep")
        with pytest.raises(ActionRefused):
            strict.confirm_all(note.id, "sales_rep")

        assert strict.get(account.id).state == "pending"
        assert log_of(audit)[-1] == ("sales_rep", "candidate.confirm", "refused")


def test_refused_note_is_not_saved(db_path, audit):
    with make_store(db_path, audit, {"candidate.confirm": Rule(Tier.GREEN)}) as strict:
        with pytest.raises(ActionRefused):
            strict.submit_note(NOTE, "sales_rep")
    conn = raw(db_path)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    conn.close()
