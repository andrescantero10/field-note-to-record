"""Writer: turns a reviewed note into CRM records.

This is the one module that calls the CRM adapter. For one note it writes, in
this order:

    account   always
    contact   always, linked to the account
    deal      only if at least one product was confirmed
    task      only if a follow-up was confirmed

Rules enforced here:

- A note is written only when it is ready: nothing is still waiting for
  review, and the account name and the contact name are confirmed.
- Every CRM call is preceded by its own policy check. Writing is yellow in the
  policy table: the person who taps "write" must be the owner of the record,
  which is the note's author. Anyone else is refused, and the refusal is logged.
- Nothing is written twice. The CRM id of each record is remembered in the
  crm_links table. A repeat finds the link and either does nothing (same
  content) or updates that same record (changed content).
- After everything is written, the note is locked.

What is not sent: prices, promises, and the deal's products. They stay in the app.

Known limit: the link is saved right after the CRM answers. If the app stopped
between those two steps, a retry could create a duplicate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from fieldnote.audit import AuditLog
from fieldnote.candidates import Candidate, CandidateStore, Note
from fieldnote.crm import Account, Contact, CrmClient, CrmError, Deal, Task
from fieldnote.policy import ActionRefused, ActionRequest, PolicyEngine

ACTION = "crm.upsert"

WRITTEN = "written"                # sent to the CRM for the first time
UPDATED = "updated"                # sent again because its content changed
ALREADY_WRITTEN = "already written"  # nothing sent: the CRM already has it

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_links (
    note_id      INTEGER NOT NULL REFERENCES notes(id),
    kind         TEXT NOT NULL,    -- account, contact, deal or task
    crm_id       TEXT NOT NULL,    -- the record's id in the CRM
    payload_hash TEXT NOT NULL,    -- fingerprint of what was sent
    written_at   TEXT NOT NULL,
    PRIMARY KEY (note_id, kind)
);
"""

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class NotReady(ValueError):
    """The note cannot be written yet. problems says why, one line each."""

    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("the note is not ready to write: " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class WrittenRecord:
    kind: str    # "account", "contact", "deal" or "task"
    crm_id: str
    result: str  # WRITTEN, UPDATED or ALREADY_WRITTEN


@dataclass(frozen=True)
class WriteResult:
    records: tuple[WrittenRecord, ...]
    locked: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(record: object) -> str:
    """A short, fixed-length summary of a record's content. Two records have
    the same fingerprint only if their content is the same."""
    fields = asdict(record)
    if isinstance(fields.get("due_date"), date):
        fields["due_date"] = fields["due_date"].isoformat()
    text = json.dumps([type(record).__name__, fields], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_date(text: str) -> date | None:
    """The date, if text is exactly a date like 2026-11-20. Otherwise None."""
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:  # looks like a date but is not one, such as 2026-13-45
        return None


@dataclass(frozen=True)
class _Reviewed:
    """What the reviewer confirmed, picked out of a note's candidates."""

    account_name: str
    contact_name: str
    email: str | None
    phone: str | None
    has_products: bool
    task_subject: str | None
    task_due: str | None


def _read_review(candidates: tuple[Candidate, ...]) -> _Reviewed:
    """Check that the note is ready and collect its confirmed values."""
    confirmed = {c.name: c.value for c in candidates if c.type == "field" and c.state == "confirmed"}
    pending = sum(1 for c in candidates if c.state == "pending")

    problems = []
    if pending:
        problems.append(f"{pending} suggestion{'s are' if pending != 1 else ' is'} still to review")
    if "account.name" not in confirmed:
        problems.append("the account name is not confirmed")
    if "contact.name" not in confirmed:
        problems.append("the contact name is not confirmed")
    if problems:
        raise NotReady(tuple(problems))

    return _Reviewed(
        account_name=confirmed["account.name"],
        contact_name=confirmed["contact.name"],
        email=confirmed.get("contact.email"),
        phone=confirmed.get("contact.phone"),
        has_products=any(c.type == "item" and c.state == "confirmed" for c in candidates),
        task_subject=confirmed.get("task.subject"),
        task_due=confirmed.get("task.due"),
    )


def _task(review: _Reviewed, deal_id: str | None, contact_id: str) -> Task:
    """The follow-up task. The due field becomes a real due date only when the
    reviewer typed it as a date. Otherwise its words are added to the subject,
    unless they are already there."""
    subject = review.task_subject
    due_date = None
    if review.task_due is not None:
        words = review.task_due.strip()
        due_date = _as_date(words)
        if due_date is None and words.lower() not in subject.lower():
            subject = f"{subject} (due: {words})"
    return Task(subject, due_date=due_date, deal_id=deal_id, contact_id=contact_id)


class Writer:
    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        store: CandidateStore,
        crm: CrmClient,
        policy: PolicyEngine,
        audit: AuditLog,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._crm = crm
        self._policy = policy
        self._audit = audit
        self._clock = clock
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Writer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write_note(self, note_id: int, actor: str) -> WriteResult:
        """Write the note's confirmed records to the CRM and lock the note.

        Raises NotReady if the review is not finished, ActionRefused if the
        policy says no, and CrmError if the CRM fails. After a CrmError, what
        was already written is remembered, so calling again is safe.
        """
        note = self._store.get_note(note_id)
        review = _read_review(self._store.candidates(note_id))

        written = []
        account = self._write_one(note, actor, "account", Account(review.account_name))
        written.append(account)
        contact = self._write_one(
            note, actor, "contact",
            Contact(review.contact_name, email=review.email, phone=review.phone,
                    account_id=account.crm_id),
        )
        written.append(contact)

        deal_id = None
        if review.has_products:
            # Named after the account and the day the note was written.
            name = f"{review.account_name} - {note.created_at[:10]}"
            deal = self._write_one(
                note, actor, "deal",
                Deal(name, account_id=account.crm_id, contact_id=contact.crm_id),
            )
            written.append(deal)
            deal_id = deal.crm_id

        if review.task_subject is not None:
            written.append(
                self._write_one(note, actor, "task", _task(review, deal_id, contact.crm_id))
            )

        self._store.lock_note(note_id, actor)
        self._audit.append(
            actor, ACTION, "succeeded",
            {"note_id": note_id, "written": [w.kind for w in written], "note_locked": True},
        )
        return WriteResult(tuple(written), locked=True)

    def _write_one(self, note: Note, actor: str, kind: str, record: object) -> WrittenRecord:
        """Policy check, then at most one CRM call, then remember the link."""
        # The tap on "write" is the confirmation, so approved_by is the actor.
        # The policy allows it only if the actor is the record owner.
        decision = self._policy.check(
            ActionRequest(ACTION, requester=actor, record_owner=note.author, approved_by=actor,
                          details={"note_id": note.id, "kind": kind})
        )
        if not decision.allowed:
            raise ActionRefused(decision)

        fingerprint = _fingerprint(record)
        link = self._conn.execute(
            "SELECT crm_id, payload_hash FROM crm_links WHERE note_id = ? AND kind = ?",
            (note.id, kind),
        ).fetchone()

        # A task cannot be updated in the CRM, so one that exists is never sent again.
        if link is not None and (link[1] == fingerprint or kind == "task"):
            return self._done(note, actor, kind, link[0], ALREADY_WRITTEN)

        existing_id = None if link is None else link[0]
        try:
            if kind == "account":
                crm_id = self._crm.save_account(record, existing_id)
            elif kind == "contact":
                crm_id = self._crm.save_contact(record, existing_id)
            elif kind == "deal":
                crm_id = self._crm.save_deal(record, existing_id)
            else:
                crm_id = self._crm.create_task(record)
        except CrmError:
            # A fixed entry: the CRM's own message could contain a token or a URL.
            self._audit.append(actor, ACTION, "failed", {"note_id": note.id, "kind": kind})
            raise

        self._conn.execute(
            "INSERT OR REPLACE INTO crm_links (note_id, kind, crm_id, payload_hash, written_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (note.id, kind, crm_id, fingerprint, self._now()),
        )
        return self._done(note, actor, kind, crm_id, WRITTEN if link is None else UPDATED)

    def _done(self, note: Note, actor: str, kind: str, crm_id: str, result: str) -> WrittenRecord:
        self._audit.append(
            actor, ACTION, "succeeded",
            {"note_id": note.id, "kind": kind, "crm_id": crm_id, "result": result},
        )
        return WrittenRecord(kind, crm_id, result)

    def _now(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).isoformat()
