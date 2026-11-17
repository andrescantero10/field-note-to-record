"""Tests for the writer: confirmed suggestions become CRM records.

The CRM here is the mock. Nothing uses the network.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from fieldnote.audit import AuditLog
from fieldnote.candidates import CandidateStore, NotFound
from fieldnote.crm import CrmError, MockCrm
from fieldnote.extraction import check_model_output
from fieldnote.policy import ActionRefused, PolicyEngine, Rule, Tier
from fieldnote.policy_table import POLICY_TABLE
from fieldnote.writer import NotReady, Writer, WrittenRecord

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
NAMES = ["account", "contact", "email", "phone", "subject", "due", "arms", "gripper", "promise"]
CLOCK = lambda: datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
REP = "sales_rep"


class App:
    """The real modules wired together on one temporary database."""

    def __init__(self, db_path, table=POLICY_TABLE):
        self.db_path = db_path
        self.audit = AuditLog(db_path, clock=CLOCK)
        self.policy = PolicyEngine(self.audit, table)
        self.store = CandidateStore(db_path, policy=self.policy, audit=self.audit,
                                    product_names=PRODUCTS, clock=CLOCK)
        self.crm = MockCrm(db_path)
        self.writer = Writer(db_path, store=self.store, crm=self.crm, policy=self.policy,
                             audit=self.audit, clock=CLOCK)

    def new_note(self, author=REP):
        """Save the worked example and its suggestions. Nothing is decided yet."""
        note = self.store.submit_note(NOTE, author)
        return dict(zip(NAMES, self.store.save_extraction(note.id, EXTRACTION), strict=True))

    def reviewed_note(self, author=REP):
        """The worked example with every suggestion decided: the gripper gets
        a product, everything else is confirmed as suggested."""
        c = self.new_note(author)
        self.store.confirm_all(c["account"].note_id, author)
        self.store.confirm(c["arms"].id, author)
        self.store.edit_item(c["gripper"].id, author, product_id="GRIP-2F-85")
        self.store.confirm(c["gripper"].id, author)
        self.store.confirm(c["promise"].id, author)
        return c

    def crm_log(self):
        """The audit entries about CRM writes, as (actor, outcome, details)."""
        return [(e.actor, e.outcome, e.details) for e in self.audit.events()
                if e.action_type == "crm.upsert"]

    def close(self):
        self.writer.close()
        self.crm.close()
        self.store.close()
        self.audit.close()


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / "app.db")
    yield a
    a.close()


def fields_of(app, module):
    return [r.fields for r in app.crm.records(module)]


# --- The normal case ----------------------------------------------------------------------


def test_a_reviewed_note_is_written_as_four_linked_records(app):
    note_id = app.reviewed_note()["account"].note_id

    result = app.writer.write_note(note_id, REP)

    assert app.crm.calls == ["save_account", "save_contact", "save_deal", "create_task"]
    (account,) = app.crm.records("Accounts")
    (contact,) = app.crm.records("Contacts")
    (deal,) = app.crm.records("Deals")
    (task,) = app.crm.records("Tasks")
    assert account.fields == {"name": "Bayside Packaging"}
    assert contact.fields == {"name": "Dana", "email": None, "phone": None, "account_id": account.id}
    assert deal.fields == {"name": "Bayside Packaging - 2026-11-16", "account_id": account.id,
                           "contact_id": contact.id}
    # "Friday" is already in the subject, so it is not repeated, and no date is sent.
    assert task.fields == {"subject": "Call her Friday", "due_date": None, "deal_id": deal.id,
                           "contact_id": contact.id}

    assert result.records == (
        WrittenRecord("account", account.id, "written"),
        WrittenRecord("contact", contact.id, "written"),
        WrittenRecord("deal", deal.id, "written"),
        WrittenRecord("task", task.id, "written"),
    )
    assert result.locked is True
    assert app.store.get_note(note_id).locked


def test_every_crm_call_comes_right_after_its_own_policy_check(app):
    note_id = app.reviewed_note()["account"].note_id
    app.writer.write_note(note_id, REP)

    log = app.crm_log()
    assert [(actor, outcome) for actor, outcome, _ in log] == [
        (REP, "allowed"), (REP, "succeeded"),  # account
        (REP, "allowed"), (REP, "succeeded"),  # contact
        (REP, "allowed"), (REP, "succeeded"),  # deal
        (REP, "allowed"), (REP, "succeeded"),  # task
        (REP, "succeeded"),                    # the note is fully written and locked
    ]
    kinds = ["account", "contact", "deal", "task"]
    for i, kind in enumerate(kinds):
        check, written = log[2 * i][2], log[2 * i + 1][2]
        assert check["tier"] == "yellow" and check["reason"] == "confirmed by the record owner"
        assert (check["record_owner"], check["approved_by"]) == (REP, REP)
        assert check["request_details"] == {"note_id": note_id, "kind": kind}
        assert (written["note_id"], written["kind"], written["result"]) == (note_id, kind, "written")
        assert written["crm_id"].startswith("mock-")
    assert log[-1][2] == {"note_id": note_id, "written": kinds, "note_locked": True}
    assert app.audit.verify().ok


def test_confirmed_email_and_phone_are_sent_and_rejected_ones_are_not(app):
    c = app.reviewed_note()
    app.store.edit_value(c["email"].id, REP, "dana@bayside.example")
    app.store.confirm(c["email"].id, REP)
    app.store.edit_value(c["phone"].id, REP, "555-0100")
    app.store.reject(c["phone"].id, REP)

    app.writer.write_note(c["account"].note_id, REP)

    (contact,) = fields_of(app, "Contacts")
    assert (contact["email"], contact["phone"]) == ("dana@bayside.example", None)


def test_nothing_about_money_or_promises_reaches_the_crm(app):
    note_id = app.reviewed_note()["account"].note_id
    app.writer.write_note(note_id, REP)

    everything = json.dumps([fields_of(app, m) for m in ("Accounts", "Contacts", "Deals", "Tasks")])
    assert "5%" not in everything and "off" not in everything
    assert "ARM-6KG" not in everything and "GRIP-2F-85" not in everything  # products stay in the app


# --- When a note is not ready -----------------------------------------------------------------


def test_a_note_with_anything_left_to_review_is_not_written(app):
    c = app.new_note()
    app.store.confirm_all(c["account"].note_id, REP)  # the items and the promise still wait

    with pytest.raises(NotReady) as caught:
        app.writer.write_note(c["account"].note_id, REP)

    assert caught.value.problems == ("3 suggestions are still to review",)
    assert "3 suggestions are still to review" in str(caught.value)
    assert app.crm.calls == [] and app.crm_log() == []
    assert not app.store.get_note(c["account"].note_id).locked


@pytest.mark.parametrize("name, problem", [
    ("account", "the account name is not confirmed"),
    ("contact", "the contact name is not confirmed"),
])
def test_account_and_contact_names_must_be_confirmed(app, name, problem):
    c = app.reviewed_note()
    app.store.reject(c[name].id, REP)

    with pytest.raises(NotReady) as caught:
        app.writer.write_note(c["account"].note_id, REP)

    assert caught.value.problems == (problem,)
    assert app.crm.calls == []


def test_every_problem_is_reported_at_once(app):
    c = app.new_note()
    with pytest.raises(NotReady) as caught:
        app.writer.write_note(c["account"].note_id, REP)
    assert caught.value.problems == (
        "7 suggestions are still to review",
        "the account name is not confirmed",
        "the contact name is not confirmed",
    )


def test_an_edit_after_review_makes_the_note_not_ready_again(app):
    c = app.reviewed_note()
    app.store.edit_value(c["account"].id, REP, "Bayside Packaging LLC")  # back to "to review"

    with pytest.raises(NotReady):
        app.writer.write_note(c["account"].note_id, REP)
    assert app.crm.calls == []


def test_an_unknown_note_is_not_found(app):
    with pytest.raises(NotFound):
        app.writer.write_note(999, REP)
    assert app.crm.calls == []


# --- Deal and task are optional -----------------------------------------------------------------


def test_no_confirmed_product_means_no_deal(app):
    c = app.reviewed_note()
    app.store.reject(c["arms"].id, REP)
    app.store.reject(c["gripper"].id, REP)

    result = app.writer.write_note(c["account"].note_id, REP)

    assert [r.kind for r in result.records] == ["account", "contact", "task"]
    assert app.crm.records("Deals") == []
    (task,) = fields_of(app, "Tasks")
    assert task["deal_id"] is None and task["contact_id"] is not None
    assert result.locked


def test_one_confirmed_product_is_enough_for_a_deal(app):
    c = app.reviewed_note()
    app.store.reject(c["gripper"].id, REP)
    app.writer.write_note(c["account"].note_id, REP)
    assert len(app.crm.records("Deals")) == 1


def test_no_confirmed_follow_up_means_no_task(app):
    c = app.reviewed_note()
    app.store.reject(c["subject"].id, REP)

    result = app.writer.write_note(c["account"].note_id, REP)

    assert [r.kind for r in result.records] == ["account", "contact", "deal"]
    assert app.crm.records("Tasks") == [] and result.locked


@pytest.mark.parametrize("due, subject, date_sent", [
    ("2026-11-20", "Call her Friday", "2026-11-20"),                 # a real date is sent as one
    ("next week", "Call her Friday (due: next week)", None),          # words go into the subject
    ("friday", "Call her Friday", None),                              # already there: not repeated
    ("2026-13-45", "Call her Friday (due: 2026-13-45)", None),        # not a real date: just words
    ("20/11/2026", "Call her Friday (due: 20/11/2026)", None),
    ("20261120", "Call her Friday (due: 20261120)", None),            # only the 2026-11-20 form
    (" 2026-11-20 ", "Call her Friday", "2026-11-20"),                # spaces around it are ignored
    ("  next week ", "Call her Friday (due: next week)", None),
])
def test_the_due_field_is_a_date_only_when_it_is_typed_as_one(app, due, subject, date_sent):
    c = app.reviewed_note()
    app.store.edit_value(c["due"].id, REP, due)
    app.store.confirm(c["due"].id, REP)

    app.writer.write_note(c["account"].note_id, REP)

    (task,) = fields_of(app, "Tasks")
    assert (task["subject"], task["due_date"]) == (subject, date_sent)


def test_a_rejected_due_is_left_out(app):
    c = app.reviewed_note()
    app.store.edit_value(c["due"].id, REP, "next week")
    app.store.reject(c["due"].id, REP)
    app.writer.write_note(c["account"].note_id, REP)
    (task,) = fields_of(app, "Tasks")
    assert (task["subject"], task["due_date"]) == ("Call her Friday", None)


# --- Policy: only the note's author can write it ------------------------------------------------------


def test_someone_else_cannot_write_the_note(app):
    note_id = app.reviewed_note()["account"].note_id

    with pytest.raises(ActionRefused) as caught:
        app.writer.write_note(note_id, "owner")

    assert caught.value.decision.reason == "confirmed by someone other than the record owner"
    assert app.crm.calls == []
    assert all(app.crm.records(m) == [] for m in ("Accounts", "Contacts", "Deals", "Tasks"))
    (entry,) = app.crm_log()
    assert (entry[0], entry[1]) == ("owner", "refused")
    assert entry[2]["tier"] == "yellow" and entry[2]["record_owner"] == REP
    assert not app.store.get_note(note_id).locked


def test_the_owner_can_write_his_own_note(app):
    note_id = app.reviewed_note(author="owner")["account"].note_id
    assert app.writer.write_note(note_id, "owner").locked
    with pytest.raises(ActionRefused):
        app.writer.write_note(note_id, REP)  # and the rep cannot touch it, even afterwards


@pytest.mark.parametrize("table", [
    {k: v for k, v in POLICY_TABLE.items() if k != "crm.upsert"},          # action type unknown
    {**POLICY_TABLE, "crm.upsert": Rule(Tier.RED, approver="owner")},      # needs someone else
], ids=["not_in_table", "red"])
def test_a_policy_refusal_stops_the_write_before_the_crm_is_called(tmp_path, table):
    app = App(tmp_path / "app.db", table)
    try:
        note_id = app.reviewed_note()["account"].note_id
        with pytest.raises(ActionRefused):
            app.writer.write_note(note_id, REP)
        assert app.crm.calls == []
        assert [outcome for _, outcome, _ in app.crm_log()] == ["refused"]
        assert not app.store.get_note(note_id).locked
    finally:
        app.close()


# --- Never twice ----------------------------------------------------------------------------------------


def test_writing_twice_writes_once(app):
    note_id = app.reviewed_note()["account"].note_id
    first = app.writer.write_note(note_id, REP)
    calls_after_first = list(app.crm.calls)
    entries_after_first = len(app.crm_log())

    second = app.writer.write_note(note_id, REP)

    assert app.crm.calls == calls_after_first  # the CRM was not called again
    assert [len(app.crm.records(m)) for m in ("Accounts", "Contacts", "Deals", "Tasks")] == [1, 1, 1, 1]
    assert [(r.kind, r.crm_id) for r in second.records] == [(r.kind, r.crm_id) for r in first.records]
    assert [r.result for r in second.records] == ["already written"] * 4
    assert second.locked

    new_entries = app.crm_log()[entries_after_first:]
    written = [d for _, outcome, d in new_entries if outcome == "succeeded" and "kind" in d]
    assert [d["result"] for d in written] == ["already written"] * 4


def test_the_links_are_kept_one_per_note_and_kind(app, tmp_path):
    note_id = app.reviewed_note()["account"].note_id
    result = app.writer.write_note(note_id, REP)

    conn = sqlite3.connect(app.db_path)
    rows = conn.execute("SELECT note_id, kind, crm_id, written_at FROM crm_links ORDER BY rowid").fetchall()
    assert rows == [(note_id, r.kind, r.crm_id, "2026-11-16T15:00:00+00:00") for r in result.records]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO crm_links (note_id, kind, crm_id, payload_hash, written_at)"
                     " VALUES (?, 'account', 'mock-99', 'x', 'now')", (note_id,))
    conn.close()


def test_two_notes_about_the_same_person_share_the_contact_by_email(app):
    for _ in range(2):
        c = app.reviewed_note()
        app.store.edit_value(c["email"].id, REP, "dana@bayside.example")
        app.store.confirm(c["email"].id, REP)
        app.writer.write_note(c["account"].note_id, REP)

    assert len(app.crm.records("Contacts")) == 1
    # Known limit: the account name is not unique in the CRM, so a second note
    # about the same company makes a second account.
    assert len(app.crm.records("Accounts")) == 2


# --- When the CRM fails ---------------------------------------------------------------------------------


def fail_once(monkeypatch, crm, action):
    """Make one CRM action fail the next time it is called."""
    def boom(*args, **kwargs):
        raise CrmError("the CRM is down")
    monkeypatch.setattr(crm, action, boom)


def test_a_failure_halfway_is_logged_and_a_retry_finishes_without_duplicates(app, monkeypatch):
    note_id = app.reviewed_note()["account"].note_id
    fail_once(monkeypatch, app.crm, "save_deal")

    with pytest.raises(CrmError):
        app.writer.write_note(note_id, REP)

    assert [len(app.crm.records(m)) for m in ("Accounts", "Contacts", "Deals", "Tasks")] == [1, 1, 0, 0]
    assert not app.store.get_note(note_id).locked
    actor, outcome, details = app.crm_log()[-1]
    assert (actor, outcome, details) == (REP, "failed", {"note_id": note_id, "kind": "deal"})
    assert "down" not in json.dumps(details)  # the CRM's own message is not copied into the log

    monkeypatch.undo()  # the CRM works again
    result = app.writer.write_note(note_id, REP)

    assert [r.result for r in result.records] == ["already written", "already written", "written", "written"]
    assert [len(app.crm.records(m)) for m in ("Accounts", "Contacts", "Deals", "Tasks")] == [1, 1, 1, 1]
    assert result.locked and app.store.get_note(note_id).locked


def test_a_failure_on_the_first_call_writes_nothing(app):
    note_id = app.reviewed_note()["account"].note_id
    app.crm.fail_next = 1

    with pytest.raises(CrmError):
        app.writer.write_note(note_id, REP)

    assert all(app.crm.records(m) == [] for m in ("Accounts", "Contacts", "Deals", "Tasks"))
    assert app.writer.write_note(note_id, REP).locked
    assert len(app.crm.records("Accounts")) == 1


def test_a_change_after_a_failed_write_updates_the_same_record(app, monkeypatch):
    c = app.reviewed_note()
    note_id = c["account"].note_id
    fail_once(monkeypatch, app.crm, "save_deal")
    with pytest.raises(CrmError):
        app.writer.write_note(note_id, REP)
    monkeypatch.undo()
    (account_before,) = app.crm.records("Accounts")

    app.store.edit_value(c["account"].id, REP, "Bayside Packaging LLC")
    app.store.confirm(c["account"].id, REP)
    result = app.writer.write_note(note_id, REP)

    (account_after,) = app.crm.records("Accounts")  # still one account
    assert account_after.id == account_before.id
    assert account_after.fields == {"name": "Bayside Packaging LLC"}
    assert [r.result for r in result.records] == ["updated", "already written", "written", "written"]
    assert fields_of(app, "Deals")[0]["name"] == "Bayside Packaging LLC - 2026-11-16"


def test_a_written_note_cannot_change_any_more(app):
    c = app.reviewed_note()
    app.writer.write_note(c["account"].note_id, REP)
    from fieldnote.candidates import NoteLocked
    with pytest.raises(NoteLocked):
        app.store.edit_value(c["account"].id, REP, "Changed after writing")
    assert fields_of(app, "Accounts") == [{"name": "Bayside Packaging"}]


def test_hostile_text_is_written_as_plain_text(app):
    c = app.reviewed_note()
    text = "Robert'); DROP TABLE crm_links;-- <script>alert(1)</script>"
    app.store.edit_value(c["account"].id, REP, text)
    app.store.confirm(c["account"].id, REP)

    app.writer.write_note(c["account"].note_id, REP)

    assert fields_of(app, "Accounts") == [{"name": text}]
    assert app.writer.write_note(c["account"].note_id, REP).records[0].result == "already written"


def test_a_task_is_never_created_twice(app, monkeypatch):
    # The task is the last record. Suppose locking fails right after it was
    # created, and the reviewer then changes the due field and writes again.
    c = app.reviewed_note()
    note_id = c["account"].note_id

    def lock_fails(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(app.store, "lock_note", lock_fails)
    with pytest.raises(sqlite3.OperationalError):
        app.writer.write_note(note_id, REP)
    monkeypatch.undo()
    assert len(app.crm.records("Tasks")) == 1 and not app.store.get_note(note_id).locked

    app.store.edit_value(c["due"].id, REP, "next week")
    app.store.confirm(c["due"].id, REP)
    result = app.writer.write_note(note_id, REP)

    assert len(app.crm.records("Tasks")) == 1  # a task cannot be updated, so it is left as it was
    assert result.records[-1].result == "already written" and result.locked
