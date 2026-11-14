"""Append-only audit log.

Every action the app takes is recorded here. Entries can be added and read,
never changed or deleted. Three layers enforce that:

1. This module offers no update or delete operation.
2. SQLite triggers abort any UPDATE or DELETE on the table, even from raw SQL.
3. Each entry stores a SHA-256 hash that covers its own contents and the
   previous entry's hash. verify() recomputes the chain, so an edit made
   directly to the database file (with the triggers removed) is detected.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

# The only outcomes an entry may have. Anything else is rejected.
OUTCOMES = frozenset({"allowed", "refused", "succeeded", "failed"})

# The "previous hash" of the very first entry, since nothing comes before it.
GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action_type TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    details     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;
"""


@dataclass(frozen=True)
class AuditEvent:
    """One entry, as stored. frozen=True means the object cannot be modified."""

    id: int
    timestamp: str
    actor: str
    action_type: str
    outcome: str
    details: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerificationResult:
    """ok is True when the whole chain checks out; otherwise first_bad_id names
    the first entry whose hash or link to the previous entry is wrong."""

    ok: bool
    first_bad_id: int | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(details: dict[str, Any]) -> str:
    """Serialize details the same way every time (sorted keys, no spaces), so
    the same content always produces the same hash. Raises TypeError for
    values JSON cannot represent and ValueError for NaN/Infinity."""
    return json.dumps(
        details,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _compute_hash(
    prev_hash: str,
    timestamp: str,
    actor: str,
    action_type: str,
    outcome: str,
    details_json: str,
) -> str:
    # Encode the fields as a JSON list so field boundaries are unambiguous
    # ("ab" + "c" and "a" + "bc" give different payloads).
    payload = json.dumps(
        [prev_hash, timestamp, actor, action_type, outcome, details_json],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


class AuditLog:
    """Append-only log stored in a SQLite file.

    clock is injectable so tests can control timestamps; it must return a
    timezone-aware datetime.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._clock = clock
        # isolation_level=None: no implicit transactions; append() opens its own.
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def append(
        self,
        actor: str,
        action_type: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Add one entry and return it. All checks run before anything is
        written, so an invalid call leaves the log untouched.

        action_type is stored as given and not checked against the policy
        table: the log must also be able to record a refused unknown type.
        """
        _require_text("actor", actor)
        _require_text("action_type", action_type)
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise TypeError("details must be a dict")
        details_json = _canonical_json(details)

        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        timestamp = now.astimezone(timezone.utc).isoformat()

        # BEGIN IMMEDIATE takes the write lock before reading the last hash,
        # so two writers cannot both chain onto the same previous entry.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row else GENESIS_HASH
            entry_hash = _compute_hash(
                prev_hash, timestamp, actor, action_type, outcome, details_json
            )
            cursor = self._conn.execute(
                "INSERT INTO audit_events"
                " (timestamp, actor, action_type, outcome, details, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp, actor, action_type, outcome, details_json, prev_hash, entry_hash),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

        return AuditEvent(
            id=cursor.lastrowid,
            timestamp=timestamp,
            actor=actor,
            action_type=action_type,
            outcome=outcome,
            details=json.loads(details_json),
            prev_hash=prev_hash,
            hash=entry_hash,
        )

    def events(self) -> list[AuditEvent]:
        """All entries, oldest first."""
        rows = self._conn.execute(
            "SELECT id, timestamp, actor, action_type, outcome, details, prev_hash, hash"
            " FROM audit_events ORDER BY id"
        ).fetchall()
        return [
            AuditEvent(
                id=r[0],
                timestamp=r[1],
                actor=r[2],
                action_type=r[3],
                outcome=r[4],
                details=json.loads(r[5]),
                prev_hash=r[6],
                hash=r[7],
            )
            for r in rows
        ]

    def verify(self) -> VerificationResult:
        """Walk the chain from the first entry and recompute every hash from
        the stored text. Returns the first entry that does not match."""
        expected_prev = GENESIS_HASH
        rows = self._conn.execute(
            "SELECT id, timestamp, actor, action_type, outcome, details, prev_hash, hash"
            " FROM audit_events ORDER BY id"
        )
        for id_, timestamp, actor, action_type, outcome, details_json, prev_hash, stored_hash in rows:
            recomputed = _compute_hash(
                prev_hash, timestamp, actor, action_type, outcome, details_json
            )
            if prev_hash != expected_prev or recomputed != stored_hash:
                return VerificationResult(ok=False, first_bad_id=id_)
            expected_prev = stored_hash
        return VerificationResult(ok=True)
