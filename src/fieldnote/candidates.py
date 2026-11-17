"""Candidate store: saves notes and the suggestions extracted from them, and
tracks what the reviewer decides about each suggestion.

A candidate is one reviewable thing: a field, a deal item, or a commitment.
Its state is one of:

    not_stated  the note does not say it (a person may fill it in)
    pending     waiting for the reviewer
    confirmed
    rejected

Rules enforced here, so no other part of the app can skip them:

- Commitments, low-confidence candidates and anything a person edited must be
  confirmed one at a time. confirm_all() never confirms them.
- An edit does not confirm. It puts the candidate back to pending, and the
  reviewer confirms it as a second step.
- Only the person who wrote a note may confirm, reject or edit what was
  extracted from it. Anyone else is refused, and the refusal is logged.
- Every confirm, reject and edit passes the policy check first, which also
  writes it to the audit log.
- After a note is locked, nothing about it can change.

Three rules are also enforced by the database itself (SQLite triggers), so
they hold even for raw SQL: a saved note cannot be changed or deleted, what
the AI originally suggested cannot be overwritten, and candidates of a locked
note cannot be updated.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from fieldnote.audit import AuditLog
from fieldnote.extraction import MAX_NOTE_CHARS, MAX_QUANTITY, Extraction
from fieldnote.policy import ActionRefused, ActionRequest, PolicyEngine

MAX_VALUE_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    author     TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS notes_no_update BEFORE UPDATE ON notes
BEGIN SELECT RAISE(ABORT, 'a saved note cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS notes_no_delete BEFORE DELETE ON notes
BEGIN SELECT RAISE(ABORT, 'a saved note cannot be changed'); END;

CREATE TABLE IF NOT EXISTS note_locks (
    note_id   INTEGER PRIMARY KEY REFERENCES notes(id),
    locked_by TEXT NOT NULL,
    locked_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS note_locks_no_update BEFORE UPDATE ON note_locks
BEGIN SELECT RAISE(ABORT, 'a lock cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS note_locks_no_delete BEFORE DELETE ON note_locks
BEGIN SELECT RAISE(ABORT, 'a lock cannot be changed'); END;

CREATE TABLE IF NOT EXISTS candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id             INTEGER NOT NULL REFERENCES notes(id),
    position            INTEGER NOT NULL,
    type                TEXT NOT NULL,
    name                TEXT NOT NULL,
    -- What the AI suggested. Never changed after it is saved.
    original_value      TEXT,
    original_product_id TEXT,
    original_quantity   INTEGER,
    span_start          INTEGER,
    span_end            INTEGER,
    confidence          TEXT,
    reasons             TEXT NOT NULL,
    -- The current value and the reviewer's decision.
    value               TEXT,
    product_id          TEXT,
    quantity            INTEGER,
    kind                TEXT,
    edited              INTEGER NOT NULL DEFAULT 0,
    state               TEXT NOT NULL,
    decided_by          TEXT,
    decided_at          TEXT
);
CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates
BEGIN SELECT RAISE(ABORT, 'a candidate cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS candidates_original_fixed
BEFORE UPDATE OF note_id, position, type, name, original_value, original_product_id,
                 original_quantity, span_start, span_end, confidence, reasons
ON candidates
BEGIN SELECT RAISE(ABORT, 'what the AI suggested cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS candidates_locked BEFORE UPDATE ON candidates
WHEN EXISTS (SELECT 1 FROM note_locks WHERE note_id = OLD.note_id)
BEGIN SELECT RAISE(ABORT, 'the note is locked'); END;
"""

_COLUMNS = (
    "id, note_id, type, name, state, value, product_id, quantity, kind, edited, "
    "original_value, original_product_id, original_quantity, span_start, span_end, "
    "confidence, reasons, decided_by, decided_at"
)


NOT_AUTHOR = "only the note's author may review it"


class NoteLocked(Exception):
    """The note is locked, so nothing about it can change any more."""


class NotAuthor(Exception):
    """Someone other than the note's author tried to review it."""


class NotFound(LookupError):
    """There is no note or candidate with that id."""


@dataclass(frozen=True)
class Note:
    id: int
    author: str
    text: str
    created_at: str
    locked: bool


@dataclass(frozen=True)
class Candidate:
    id: int
    note_id: int
    type: str  # "field", "item" or "commitment"
    name: str  # "account.name", ..., "item" or "commitment"
    state: str
    value: str | None  # text of a field or commitment; None for items
    product_id: str | None  # items only; None = no product picked yet
    quantity: int | None  # items only
    kind: str | None
    edited: bool  # True = the current value was typed by a person
    original_value: str | None
    original_product_id: str | None
    original_quantity: int | None
    original_span: tuple[int, int] | None
    original_confidence: str | None
    original_reasons: tuple[str, ...]
    decided_by: str | None
    decided_at: str | None

    @property
    def span(self) -> tuple[int, int] | None:
        """Where the current value is in the note. None once a person has
        edited it, because the value no longer comes from the note."""
        return None if self.edited else self.original_span

    @property
    def needs_individual_confirmation(self) -> bool:
        return self.edited or self.kind == "commitment" or self.original_confidence == "low"


@dataclass(frozen=True)
class BulkResult:
    confirmed: tuple[int, ...]  # ids confirmed by this call
    waiting: tuple[Candidate, ...]  # still pending: each needs its own confirm


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_candidate(row: tuple) -> Candidate:
    (id_, note_id, type_, name, state, value, product_id, quantity, kind, edited,
     original_value, original_product_id, original_quantity, span_start, span_end,
     confidence, reasons, decided_by, decided_at) = row
    return Candidate(
        id=id_,
        note_id=note_id,
        type=type_,
        name=name,
        state=state,
        value=value,
        product_id=product_id,
        quantity=quantity,
        kind=kind,
        edited=bool(edited),
        original_value=original_value,
        original_product_id=original_product_id,
        original_quantity=original_quantity,
        original_span=None if span_start is None else (span_start, span_end),
        original_confidence=confidence,
        original_reasons=tuple(json.loads(reasons)),
        decided_by=decided_by,
        decided_at=decided_at,
    )


class CandidateStore:
    """product_names maps catalog product ids to names (Catalog.product_names()).
    It is used to check that an edited item points at a real product."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        policy: PolicyEngine,
        audit: AuditLog,
        product_names: Mapping[str, str],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._product_names = dict(product_names)
        self._clock = clock
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CandidateStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _now(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).isoformat()

    # --- Notes ----------------------------------------------------------------

    def submit_note(self, text: str, author: str) -> Note:
        """Save a note. It can never be changed afterwards."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("note must be non-blank text")
        if len(text) > MAX_NOTE_CHARS:
            raise ValueError(f"note is longer than {MAX_NOTE_CHARS} characters")

        decision = self._policy.check(
            ActionRequest("note.submit", requester=author, record_owner=author,
                          details={"note_chars": len(text)})
        )
        if not decision.allowed:
            raise ActionRefused(decision)

        cursor = self._conn.execute(
            "INSERT INTO notes (author, text, created_at) VALUES (?, ?, ?)",
            (author, text, self._now()),
        )
        self._audit.append(author, "note.submit", "succeeded", {"note_id": cursor.lastrowid})
        return self.get_note(cursor.lastrowid)

    def get_note(self, note_id: int) -> Note:
        row = self._conn.execute(
            "SELECT id, author, text, created_at,"
            " EXISTS (SELECT 1 FROM note_locks WHERE note_id = notes.id)"
            " FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no note with id {note_id}")
        return Note(row[0], row[1], row[2], row[3], bool(row[4]))

    def lock_note(self, note_id: int, actor: str) -> None:
        """Freeze a note and its candidates for good. Locking twice is harmless."""
        self.get_note(note_id)
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be a non-blank string")
        self._conn.execute(
            "INSERT OR IGNORE INTO note_locks (note_id, locked_by, locked_at) VALUES (?, ?, ?)",
            (note_id, actor, self._now()),
        )

    def _require_unlocked(self, note_id: int) -> Note:
        note = self.get_note(note_id)
        if note.locked:
            raise NoteLocked(f"note {note_id} is locked")
        return note

    def _require_author(self, note: Note, actor: str, action_type: str) -> None:
        """Refuse, and log the refusal, unless actor wrote the note."""
        if actor != note.author:
            self._audit.append(
                actor, action_type, "refused",
                {"check": "author", "reason": NOT_AUTHOR, "note_id": note.id,
                 "author": note.author},
            )
            raise NotAuthor(NOT_AUTHOR)

    def note_awaiting_extraction(self, note_id: int, actor: str) -> Note:
        """Return the note if actor may run an extraction on it now: it is not
        locked, actor wrote it, and it has no candidates yet. Used to try
        again after the model failed."""
        note = self._require_unlocked(note_id)
        self._require_author(note, actor, "extraction.run")
        if self.candidates(note_id):
            raise ValueError(f"candidates were already saved for note {note_id}")
        return note

    # --- Saving and reading candidates ------------------------------------------

    def save_extraction(self, note_id: int, extraction: Extraction) -> tuple[Candidate, ...]:
        """Save the result of one extraction as candidates. Once per note."""
        self._require_unlocked(note_id)
        if self.candidates(note_id):
            raise ValueError(f"candidates were already saved for note {note_id}")

        rows = []
        for f in extraction.fields:
            state = "not_stated" if f.value is None else "pending"
            rows.append(("field", f.name, state, f.value, None, None, f.kind, f.span,
                         f.confidence, f.reasons))
        for item in extraction.items:
            rows.append(("item", "item", "pending", None, item.product_id, item.quantity,
                         item.kind, item.span, item.confidence, item.reasons))
        for c in extraction.commitments:
            rows.append(("commitment", "commitment", "pending", c.value, None, None,
                         c.kind, c.span, c.confidence, c.reasons))

        # One transaction: either every candidate is saved or none is.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for position, (type_, name, state, value, product_id, quantity, kind, span,
                           confidence, reasons) in enumerate(rows):
                span_start, span_end = span if span is not None else (None, None)
                self._conn.execute(
                    "INSERT INTO candidates (note_id, position, type, name, state,"
                    " original_value, original_product_id, original_quantity,"
                    " span_start, span_end, confidence, reasons,"
                    " value, product_id, quantity, kind)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (note_id, position, type_, name, state, value, product_id, quantity,
                     span_start, span_end, confidence, json.dumps(list(reasons)),
                     value, product_id, quantity, kind),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self.candidates(note_id)

    def candidates(self, note_id: int) -> tuple[Candidate, ...]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM candidates WHERE note_id = ? ORDER BY position",
            (note_id,),
        ).fetchall()
        return tuple(_to_candidate(row) for row in rows)

    def get(self, candidate_id: int) -> Candidate:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"no candidate with id {candidate_id}")
        return _to_candidate(row)

    # --- The reviewer's decisions --------------------------------------------------

    def _check_policy(self, action_type: str, actor: str, candidate: Candidate,
                      note: Note, extra: dict) -> None:
        details = {"note_id": note.id, "candidate_id": candidate.id,
                   "name": candidate.name, **extra}
        decision = self._policy.check(
            ActionRequest(action_type, requester=actor, record_owner=note.author, details=details)
        )
        if not decision.allowed:
            raise ActionRefused(decision)

    def _decide(self, candidate_id: int, state: str, actor: str) -> Candidate:
        self._conn.execute(
            "UPDATE candidates SET state = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (state, actor, self._now(), candidate_id),
        )
        return self.get(candidate_id)

    def _confirm(self, candidate: Candidate, actor: str, bulk: bool) -> Candidate:
        note = self._require_unlocked(candidate.note_id)
        self._require_author(note, actor, "candidate.confirm")
        if candidate.state == "not_stated":
            raise ValueError("there is nothing to confirm: the note does not state this")
        if candidate.type == "item" and candidate.product_id is None:
            raise ValueError("pick a product before confirming this item")
        if candidate.state == "confirmed":
            return candidate  # a second tap changes nothing
        self._check_policy("candidate.confirm", actor, candidate, note, {"bulk": bulk})
        return self._decide(candidate.id, "confirmed", actor)

    def confirm(self, candidate_id: int, actor: str) -> Candidate:
        """Confirm one candidate."""
        return self._confirm(self.get(candidate_id), actor, bulk=False)

    def reject(self, candidate_id: int, actor: str) -> Candidate:
        """Reject one candidate."""
        candidate = self.get(candidate_id)
        note = self._require_unlocked(candidate.note_id)
        self._require_author(note, actor, "candidate.reject")
        if candidate.state == "not_stated":
            raise ValueError("there is nothing to reject: the note does not state this")
        if candidate.state == "rejected":
            return candidate
        self._check_policy("candidate.reject", actor, candidate, note, {})
        return self._decide(candidate.id, "rejected", actor)

    def confirm_all(self, note_id: int, actor: str) -> BulkResult:
        """Confirm every pending candidate that is allowed to be confirmed in
        bulk. Commitments, low-confidence candidates and edited ones are left
        pending and returned in `waiting`."""
        note = self._require_unlocked(note_id)
        self._require_author(note, actor, "candidate.confirm")
        confirmed, waiting = [], []
        for candidate in self.candidates(note_id):
            if candidate.state != "pending":
                continue
            if candidate.needs_individual_confirmation:
                waiting.append(candidate)
            else:
                self._confirm(candidate, actor, bulk=True)
                confirmed.append(candidate.id)
        return BulkResult(tuple(confirmed), tuple(waiting))

    # --- Edits ----------------------------------------------------------------------

    def _apply_edit(self, candidate_id: int, value: str | None, product_id: str | None,
                    quantity: int | None, kind: str | None) -> Candidate:
        # An edit never confirms: the candidate goes back to pending and the
        # reviewer confirms it as a separate step.
        self._conn.execute(
            "UPDATE candidates SET value = ?, product_id = ?, quantity = ?, kind = ?,"
            " edited = 1, state = 'pending', decided_by = NULL, decided_at = NULL WHERE id = ?",
            (value, product_id, quantity, kind, candidate_id),
        )
        return self.get(candidate_id)

    def edit_value(self, candidate_id: int, actor: str, value: str) -> Candidate:
        """Replace the text of a field or commitment, or fill in a field the
        note did not state. The AI's original value is kept beside it."""
        candidate = self.get(candidate_id)
        note = self._require_unlocked(candidate.note_id)
        self._require_author(note, actor, "candidate.edit")
        if candidate.type == "item":
            raise ValueError("a deal item is edited with edit_item")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("the new value must be non-blank text")
        if len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"the new value is longer than {MAX_VALUE_CHARS} characters")
        if value == candidate.value:
            return candidate  # nothing changed

        # A value typed by the salesperson is something he noted himself.
        kind = candidate.kind or "observed"
        self._check_policy("candidate.edit", actor, candidate, note,
                           {"from": candidate.value, "to": value})
        return self._apply_edit(candidate.id, value, None, None, kind)

    def edit_item(self, candidate_id: int, actor: str, *, product_id: str | None = None,
                  quantity: int | None = None) -> Candidate:
        """Change the product, the quantity, or both, of a deal item."""
        candidate = self.get(candidate_id)
        note = self._require_unlocked(candidate.note_id)
        self._require_author(note, actor, "candidate.edit")
        if candidate.type != "item":
            raise ValueError("only a deal item has a product and a quantity")
        if product_id is None and quantity is None:
            raise ValueError("give a product_id, a quantity, or both")
        if product_id is not None and (
            not isinstance(product_id, str) or product_id not in self._product_names
        ):
            raise ValueError(f"{product_id!r} is not a product id in the catalog")
        if quantity is not None and (
            isinstance(quantity, bool) or not isinstance(quantity, int)
            or not 1 <= quantity <= MAX_QUANTITY
        ):
            raise ValueError("quantity must be a whole number of at least 1")

        new_product = candidate.product_id if product_id is None else product_id
        new_quantity = candidate.quantity if quantity is None else quantity
        if (new_product, new_quantity) == (candidate.product_id, candidate.quantity):
            return candidate  # nothing changed

        self._check_policy(
            "candidate.edit", actor, candidate, note,
            {"from": {"product_id": candidate.product_id, "quantity": candidate.quantity},
             "to": {"product_id": new_product, "quantity": new_quantity}},
        )
        return self._apply_edit(candidate.id, None, new_product, new_quantity, candidate.kind)
