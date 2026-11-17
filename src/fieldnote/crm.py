"""CRM adapter: the only module that will talk to Zoho CRM.

It offers four actions and no others:

    save_account    save_contact    save_deal    create_task

There is deliberately no action for quotes or invoices, none that sends
anything to a customer, and none that deletes a record.

This module does not check the policy. The module that calls it must do that
first, and tests/test_boundaries.py limits which modules may import this one.

Two clients share the same four actions:

    MockCrm   a pretend CRM kept in the local database. The default.
    (live)    the real Zoho client. Not built yet (Task 10b).

The mock copies the real CRM where it matters for our code:
- A contact's email is unique. Saving a contact whose email already exists
  updates that contact instead of making a second one.
- Accounts and deals have no unique field. Saving the same one twice without
  an id makes two records. Preventing that is the caller's job: it must
  remember the id it got back and pass it as existing_id the next time.
- A link to another record (a contact's account, a deal's contact) must point
  at a real record of the right kind.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

ACCOUNTS, CONTACTS, DEALS, TASKS = "Accounts", "Contacts", "Deals", "Tasks"
MODULES = (ACCOUNTS, CONTACTS, DEALS, TASKS)

MODE_SETTING = "ZOHO_MODE"
DEFAULT_MODE = "mock"
KNOWN_MODES = ("live", "mock")


class CrmError(Exception):
    """The CRM refused or failed. Nothing was saved by the call that raised it."""


# --- The records we send ---------------------------------------------------------------
# Plain data, checked when it is made. None of them has a money field.


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-blank text")


def _optional_text(name: str, value: object) -> None:
    if value is not None:
        _text(name, value)


@dataclass(frozen=True)
class Account:
    name: str

    def __post_init__(self) -> None:
        _text("name", self.name)


@dataclass(frozen=True)
class Contact:
    name: str
    email: str | None = None
    phone: str | None = None
    account_id: str | None = None

    def __post_init__(self) -> None:
        _text("name", self.name)
        _optional_text("email", self.email)
        _optional_text("phone", self.phone)
        _optional_text("account_id", self.account_id)


@dataclass(frozen=True)
class Deal:
    name: str
    account_id: str | None = None
    contact_id: str | None = None

    def __post_init__(self) -> None:
        _text("name", self.name)
        _optional_text("account_id", self.account_id)
        _optional_text("contact_id", self.contact_id)


@dataclass(frozen=True)
class Task:
    subject: str
    due_date: date | None = None
    deal_id: str | None = None
    contact_id: str | None = None

    def __post_init__(self) -> None:
        _text("subject", self.subject)
        # A datetime is also a date in Python, so it is ruled out by name.
        if self.due_date is not None and (
            not isinstance(self.due_date, date) or isinstance(self.due_date, datetime)
        ):
            raise ValueError("due_date must be a date")
        _optional_text("deal_id", self.deal_id)
        _optional_text("contact_id", self.contact_id)


# --- The four actions ----------------------------------------------------------------------


@runtime_checkable
class CrmClient(Protocol):
    """What every CRM client offers. Each action returns the record's id.

    existing_id says "update this record". Without it a new record is made,
    except for a contact whose email is already in the CRM.
    """

    def save_account(self, account: Account, existing_id: str | None = None) -> str: ...

    def save_contact(self, contact: Contact, existing_id: str | None = None) -> str: ...

    def save_deal(self, deal: Deal, existing_id: str | None = None) -> str: ...

    def create_task(self, task: Task) -> str: ...


# --- The mock --------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mock_crm_records (
    number    INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused
    module    TEXT NOT NULL,
    email_key TEXT,                               -- contacts: the email in lower case
    fields    TEXT NOT NULL                       -- the record, as JSON
);
CREATE UNIQUE INDEX IF NOT EXISTS mock_crm_one_contact_per_email
    ON mock_crm_records (email_key) WHERE module = 'Contacts' AND email_key IS NOT NULL;
"""

_ID_PREFIX = "mock-"  # so a mock id can never be mistaken for a real Zoho id


@dataclass(frozen=True)
class CrmRecord:
    """A record as the mock holds it. For tests and for looking at the mock."""

    id: str
    module: str
    fields: dict[str, Any]


def _number(record_id: object) -> int | None:
    """The row number inside a mock id, or None if it is not a mock id."""
    if not isinstance(record_id, str) or not record_id.startswith(_ID_PREFIX):
        return None
    digits = record_id[len(_ID_PREFIX):]
    return int(digits) if digits.isascii() and digits.isdigit() and len(digits) <= 15 else None


class MockCrm:
    """A pretend CRM in a SQLite file.

    For tests: set fail_next = N to make the next N actions raise CrmError
    without saving anything. calls lists every action attempted, in order.
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.executescript(_SCHEMA)
        self.fail_next = 0
        self.calls: list[str] = []

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MockCrm:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Looking at what the mock holds ---

    def get(self, record_id: str) -> CrmRecord:
        row = self._conn.execute(
            "SELECT module, fields FROM mock_crm_records WHERE number = ?", (_number(record_id),)
        ).fetchone()
        if row is None:
            raise CrmError(f"no record with id {record_id!r}")
        return CrmRecord(record_id, row[0], json.loads(row[1]))

    def records(self, module: str) -> list[CrmRecord]:
        if module not in MODULES:
            raise ValueError(f"unknown module {module!r}; known modules: {list(MODULES)}")
        rows = self._conn.execute(
            "SELECT number, fields FROM mock_crm_records WHERE module = ? ORDER BY number",
            (module,),
        ).fetchall()
        return [CrmRecord(f"{_ID_PREFIX}{number}", module, json.loads(fields)) for number, fields in rows]

    # --- Helpers ---

    def _begin(self, action: str, record: object, expected: type) -> None:
        """Every action starts here: check the record's type, remember the
        call, and fail if a test asked for a failure."""
        if not isinstance(record, expected):
            raise TypeError(f"{action} needs a {expected.__name__}")
        self.calls.append(action)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise CrmError("the mock CRM was told to fail")

    def _require(self, record_id: str | None, module: str) -> None:
        """An id we were given must be a real record of this module."""
        if record_id is None:
            return
        row = self._conn.execute(
            "SELECT module FROM mock_crm_records WHERE number = ?", (_number(record_id),)
        ).fetchone()
        if row is None or row[0] != module:
            raise CrmError(f"no {module} record with id {record_id!r}")

    def _save(self, module: str, record: object, existing_id: str | None,
              email_key: str | None = None) -> str:
        fields = asdict(record)
        if isinstance(fields.get("due_date"), date):
            fields["due_date"] = fields["due_date"].isoformat()
        text = json.dumps(fields)
        if existing_id is not None:
            self._require(existing_id, module)
            self._conn.execute(
                "UPDATE mock_crm_records SET fields = ?, email_key = ? WHERE number = ?",
                (text, email_key, _number(existing_id)),
            )
            return existing_id
        cursor = self._conn.execute(
            "INSERT INTO mock_crm_records (module, email_key, fields) VALUES (?, ?, ?)",
            (module, email_key, text),
        )
        return f"{_ID_PREFIX}{cursor.lastrowid}"

    # --- The four actions ---

    def save_account(self, account: Account, existing_id: str | None = None) -> str:
        self._begin("save_account", account, Account)
        return self._save(ACCOUNTS, account, existing_id)

    def save_contact(self, contact: Contact, existing_id: str | None = None) -> str:
        self._begin("save_contact", contact, Contact)
        self._require(contact.account_id, ACCOUNTS)
        self._require(existing_id, CONTACTS)

        email_key = contact.email.lower() if contact.email else None
        if email_key is not None:
            row = self._conn.execute(
                "SELECT number FROM mock_crm_records WHERE module = ? AND email_key = ?",
                (CONTACTS, email_key),
            ).fetchone()
            owner = None if row is None else f"{_ID_PREFIX}{row[0]}"
            if existing_id is None:
                existing_id = owner  # the same email is the same contact
            elif owner is not None and owner != existing_id:
                raise CrmError("another contact already has that email")
        return self._save(CONTACTS, contact, existing_id, email_key)

    def save_deal(self, deal: Deal, existing_id: str | None = None) -> str:
        self._begin("save_deal", deal, Deal)
        self._require(deal.account_id, ACCOUNTS)
        self._require(deal.contact_id, CONTACTS)
        return self._save(DEALS, deal, existing_id)

    def create_task(self, task: Task) -> str:
        self._begin("create_task", task, Task)
        self._require(task.deal_id, DEALS)
        self._require(task.contact_id, CONTACTS)
        return self._save(TASKS, task, None)


# --- Choosing the client ------------------------------------------------------------------------


def crm_mode(env: Mapping[str, str]) -> str:
    """The mode named by ZOHO_MODE in env. A missing setting means the mock.

    Any other value must match a known mode exactly. There is no fallback, so
    a typo cannot silently turn a live setup into the mock or the other way.
    """
    mode = env.get(MODE_SETTING, DEFAULT_MODE)
    if mode not in KNOWN_MODES:
        # Name only the setting that is wrong. Never echo env: it holds secrets.
        raise ValueError(f"unknown {MODE_SETTING} {mode!r}; known modes: {list(KNOWN_MODES)}")
    if mode == "live":
        raise NotImplementedError(
            f"{MODE_SETTING}=live is not built yet; use {MODE_SETTING}=mock"
        )
    return mode


def get_crm(env: Mapping[str, str], db_path: str | os.PathLike[str]) -> CrmClient:
    """The CRM client for the mode in env (pass the settings from
    fieldnote.config.load_settings())."""
    crm_mode(env)  # raises unless the mode is one we can run
    return MockCrm(db_path)
