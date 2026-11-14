"""Tests for the append-only audit log."""

import sqlite3
from datetime import datetime, timezone

import pytest

from fieldnote.audit import GENESIS_HASH, AuditLog

FIXED_TIME = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    # tmp_path is a fresh temporary folder per test, deleted by pytest.
    return tmp_path / "audit.db"


@pytest.fixture
def log(db_path):
    with AuditLog(db_path, clock=lambda: FIXED_TIME) as audit_log:
        yield audit_log


def add_three(log):
    log.append("sales_rep", "note.submit", "succeeded", {"note_id": 1})
    log.append("system", "crm.upsert", "refused", {"reason": "not confirmed"})
    log.append("owner", "quote.approve", "allowed")


def raw_connection(db_path):
    """A plain SQLite connection that bypasses AuditLog, like an attacker
    or a buggy module writing SQL directly."""
    return sqlite3.connect(db_path, isolation_level=None)


# --- Appending and reading -------------------------------------------------


def test_append_and_read_back_in_order(log):
    add_three(log)
    events = log.events()

    assert [e.id for e in events] == [1, 2, 3]
    assert [e.action_type for e in events] == ["note.submit", "crm.upsert", "quote.approve"]
    first = events[0]
    assert first.actor == "sales_rep"
    assert first.outcome == "succeeded"
    assert first.details == {"note_id": 1}
    assert first.timestamp == "2026-11-14T15:00:00+00:00"
    assert events[2].details == {}


def test_entries_are_chained(log):
    add_three(log)
    events = log.events()

    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash


def test_unknown_action_type_is_still_logged(log):
    # Deliberate: the log must be able to record that an unknown type was refused.
    log.append("system", "made.up.action", "refused", {"reason": "not in policy table"})
    assert log.events()[0].action_type == "made.up.action"


# --- Database refuses changes ----------------------------------------------


def test_raw_update_is_rejected_by_database(log, db_path):
    add_three(log)
    conn = raw_connection(db_path)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_events SET outcome = 'allowed' WHERE id = 2")
    conn.close()

    assert log.events()[1].outcome == "refused"


def test_raw_delete_is_rejected_by_database(log, db_path):
    add_three(log)
    conn = raw_connection(db_path)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_events WHERE id = 2")
    conn.close()

    assert len(log.events()) == 3


def test_api_has_no_way_to_change_entries():
    public = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert public == {"append", "events", "verify", "close"}


# --- Tamper detection -------------------------------------------------------


def test_verify_passes_on_empty_and_clean_log(log):
    assert log.verify().ok
    add_three(log)
    assert log.verify().ok


def test_verify_detects_edited_entry(log, db_path):
    add_three(log)
    conn = raw_connection(db_path)
    conn.execute("DROP TRIGGER audit_events_no_update")
    conn.execute("""UPDATE audit_events SET details = '{"reason":"confirmed"}' WHERE id = 2""")
    conn.close()

    result = log.verify()
    assert not result.ok
    assert result.first_bad_id == 2


def test_verify_detects_deleted_middle_entry(log, db_path):
    add_three(log)
    conn = raw_connection(db_path)
    conn.execute("DROP TRIGGER audit_events_no_delete")
    conn.execute("DELETE FROM audit_events WHERE id = 2")
    conn.close()

    result = log.verify()
    assert not result.ok
    assert result.first_bad_id == 3  # entry 3 now points at a hash that is gone


# --- Persistence -------------------------------------------------------------


def test_reopen_keeps_entries_and_continues_chain(db_path):
    with AuditLog(db_path, clock=lambda: FIXED_TIME) as first:
        first.append("a", "x.one", "succeeded")
        first.append("a", "x.two", "succeeded")

    with AuditLog(db_path, clock=lambda: FIXED_TIME) as reopened:
        third = reopened.append("a", "x.three", "succeeded")
        events = reopened.events()

        assert len(events) == 3
        assert third.prev_hash == events[1].hash
        assert reopened.verify().ok


# --- Invalid input writes nothing --------------------------------------------


def test_unserializable_details_writes_nothing(log):
    with pytest.raises(TypeError):
        log.append("a", "x.y", "succeeded", {"bad": object()})
    assert log.events() == []


@pytest.mark.parametrize("field", ["actor", "action_type"])
@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_actor_or_action_type_is_refused(log, field, value):
    args = {"actor": "a", "action_type": "x.y", "outcome": "succeeded"}
    args[field] = value
    with pytest.raises(ValueError):
        log.append(**args)
    assert log.events() == []


def test_unknown_outcome_is_refused(log):
    with pytest.raises(ValueError, match="outcome"):
        log.append("a", "x.y", "approved-ish")
    assert log.events() == []


def test_naive_clock_is_refused(db_path):
    with AuditLog(db_path, clock=lambda: datetime(2026, 11, 14, 15, 0)) as naive:
        with pytest.raises(ValueError, match="timezone"):
            naive.append("a", "x.y", "succeeded")
        assert naive.events() == []
