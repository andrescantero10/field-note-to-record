"""Web API: the addresses the phone page calls.

This module makes no decisions of its own. Each route works out who is acting,
hands the request to the candidate store or to the extraction step, and turns
the result (or the refusal) into JSON. The rules live in those modules, so a
request cannot get around them by coming through the web.

    GET  /  /app.js  /app.css           the phone page (three fixed files)
    GET  /products                      product ids and names, never prices
    POST /notes                         save a note and extract suggestions
    GET  /notes/{id}                    the note and its suggestions
    POST /notes/{id}/extract            try the extraction again after it failed
    POST /notes/{id}/confirm-all        confirm what may be confirmed in bulk
    POST /candidates/{id}/confirm
    POST /candidates/{id}/reject
    POST /candidates/{id}/value         replace the text of a field or commitment
    POST /candidates/{id}/item          change an item's product or quantity

Who is acting: every request names one of the fixed users in the X-Acting-As
header. There is no login. This is a limit of the prototype: anyone who can
reach the server can say he is either user.

Nothing here handles a price, and nothing here writes to the CRM.

Requests are handled one at a time (a lock). Each one opens the database, does
its work and closes it again.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from fieldnote import __version__
from fieldnote.audit import AuditLog
from fieldnote.candidates import Candidate, CandidateStore, NotAuthor, NoteLocked, NotFound
from fieldnote.catalog import Catalog
from fieldnote.extraction import ExtractionFailed, extract
from fieldnote.llm import ModelProvider
from fieldnote.policy import ActionRefused, PolicyEngine, Rule
from fieldnote.policy_table import POLICY_TABLE

# The people who can use the prototype. "owner" is also the approver named in
# the policy table.
USERS = ("sales_rep", "owner")

MAX_ID = 2**63 - 1  # the largest id SQLite can store

# The phone page: address -> file in the static folder. Only these are served.
STATIC = FilePath(__file__).parent / "static"
PAGE_FILES = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
PAGE_HEADERS = {
    # The browser may load and run only files from this server: no inline
    # script, no script from another site. A second lock, in case text from a
    # note ever reached the page as HTML.
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

UNKNOWN_USER = "say who is acting: send the X-Acting-As header with one of " + ", ".join(USERS)
BAD_REQUEST_SHAPE = "the request is not in the expected form"
EXTRACTION_FAILED = "the note was saved, but the AI could not read it; try again"


# --- What a request may contain ---------------------------------------------------
# strict: no silent conversions ("2" is not 2, true is not 1).
# extra="forbid": a body with any other key is refused, so a request cannot
# carry along an author, a state or a price.


class _Body(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class NoteIn(_Body):
    text: str


class ValueIn(_Body):
    value: str


class ItemIn(_Body):
    product_id: str | None = None
    quantity: int | None = None


def acting_user(x_acting_as: Annotated[str | None, Header()] = None) -> str:
    """The user named in the X-Acting-As header. Exact match only."""
    if x_acting_as not in USERS:
        raise HTTPException(status_code=401, detail=UNKNOWN_USER)
    return x_acting_as


Actor = Annotated[str, Depends(acting_user)]
NoteId = Annotated[int, Path(ge=1, le=MAX_ID)]
CandidateId = Annotated[int, Path(ge=1, le=MAX_ID)]


# --- What a response contains ------------------------------------------------------


def _candidate_json(c: Candidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "note_id": c.note_id,
        "type": c.type,
        "name": c.name,
        "state": c.state,
        "value": c.value,
        "product_id": c.product_id,
        "quantity": c.quantity,
        "kind": c.kind,
        "edited": c.edited,
        "span": None if c.span is None else list(c.span),
        "needs_individual_confirmation": c.needs_individual_confirmation,
        "original": {
            "value": c.original_value,
            "product_id": c.original_product_id,
            "quantity": c.original_quantity,
            "confidence": c.original_confidence,
            "reasons": list(c.original_reasons),
        },
        "decided_by": c.decided_by,
        "decided_at": c.decided_at,
    }


def _note_json(store: CandidateStore, note_id: int) -> dict[str, Any]:
    note = store.get_note(note_id)
    return {
        "note": {
            "id": note.id,
            "author": note.author,
            "text": note.text,
            "created_at": note.created_at,
            "locked": note.locked,
        },
        "candidates": [_candidate_json(c) for c in store.candidates(note_id)],
    }


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


@dataclass(frozen=True)
class _Services:
    store: CandidateStore
    policy: PolicyEngine
    audit: AuditLog


def create_app(
    *,
    db_path: str | os.PathLike[str],
    provider: ModelProvider,
    catalog: Catalog,
    table: Mapping[str, Rule] = POLICY_TABLE,
) -> FastAPI:
    """Build the web app. Tests pass a temporary database and the fake provider."""
    app = FastAPI(title="Field Note to Record", version=__version__, redoc_url=None)
    one_at_a_time = threading.Lock()
    product_names = catalog.product_names()

    @contextmanager
    def open_services() -> Iterator[_Services]:
        """Wait for our turn, open the database, and close it afterwards."""
        with one_at_a_time, AuditLog(db_path) as audit:
            policy = PolicyEngine(audit, table)
            with CandidateStore(
                db_path, policy=policy, audit=audit, product_names=product_names
            ) as store:
                yield _Services(store, policy, audit)

    def run_extraction(s: _Services, note_id: int, actor: str) -> Any:
        """Ask the model about a saved note and save what it suggests."""
        note = s.store.get_note(note_id)
        try:
            extraction = extract(note.text, actor, provider=provider, catalog=catalog,
                                 policy=s.policy, audit=s.audit)
        except ExtractionFailed:
            # A fixed message: the reason is in the audit log, not shown here.
            return JSONResponse(
                status_code=502, content={"detail": EXTRACTION_FAILED, "note_id": note.id}
            )
        s.store.save_extraction(note.id, extraction)
        return _note_json(s.store, note.id)

    # --- Refusals: turn what the store raises into an answer -----------------------

    @app.exception_handler(RequestValidationError)
    def bad_shape(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error(422, BAD_REQUEST_SHAPE)

    @app.exception_handler(NotFound)
    def not_found(request: Request, error: NotFound) -> JSONResponse:
        return _error(404, "not found")

    @app.exception_handler(NotAuthor)
    def not_author(request: Request, error: NotAuthor) -> JSONResponse:
        return _error(403, str(error))

    @app.exception_handler(ActionRefused)
    def refused_by_policy(request: Request, error: ActionRefused) -> JSONResponse:
        return _error(403, f"refused by policy: {error.decision.reason}")

    @app.exception_handler(NoteLocked)
    def locked(request: Request, error: NoteLocked) -> JSONResponse:
        return _error(409, "the note is locked and can no longer change")

    @app.exception_handler(ValueError)
    def broke_a_rule(request: Request, error: ValueError) -> JSONResponse:
        return _error(400, str(error))

    # --- The page and the product list ------------------------------------------------

    def serve(file_name: str) -> Any:
        def page_file() -> FileResponse:
            return FileResponse(STATIC / file_name, headers=PAGE_HEADERS)
        return page_file

    for address, file_name in PAGE_FILES.items():
        app.get(address, include_in_schema=False)(serve(file_name))

    @app.get("/products")
    def list_products(actor: Actor) -> Any:
        """For the "pick a product" list. Ids and names only: no prices."""
        return {
            "products": [
                {"id": product_id, "name": name}
                for product_id, name in sorted(product_names.items())
            ]
        }

    # --- Notes ---------------------------------------------------------------------

    @app.post("/notes", status_code=201)
    def submit_note(body: NoteIn, actor: Actor) -> Any:
        with open_services() as s:
            note = s.store.submit_note(body.text, actor)
            return run_extraction(s, note.id, actor)

    @app.get("/notes/{note_id}")
    def read_note(note_id: NoteId, actor: Actor) -> Any:
        with open_services() as s:
            return _note_json(s.store, note_id)

    @app.post("/notes/{note_id}/extract")
    def extract_again(note_id: NoteId, actor: Actor) -> Any:
        with open_services() as s:
            s.store.note_awaiting_extraction(note_id, actor)
            return run_extraction(s, note_id, actor)

    @app.post("/notes/{note_id}/confirm-all")
    def confirm_all(note_id: NoteId, actor: Actor) -> Any:
        with open_services() as s:
            result = s.store.confirm_all(note_id, actor)
            return {
                "confirmed": list(result.confirmed),
                "waiting": [_candidate_json(c) for c in result.waiting],
            }

    # --- One suggestion at a time -----------------------------------------------------

    @app.post("/candidates/{candidate_id}/confirm")
    def confirm(candidate_id: CandidateId, actor: Actor) -> Any:
        with open_services() as s:
            return _candidate_json(s.store.confirm(candidate_id, actor))

    @app.post("/candidates/{candidate_id}/reject")
    def reject(candidate_id: CandidateId, actor: Actor) -> Any:
        with open_services() as s:
            return _candidate_json(s.store.reject(candidate_id, actor))

    @app.post("/candidates/{candidate_id}/value")
    def edit_value(candidate_id: CandidateId, body: ValueIn, actor: Actor) -> Any:
        with open_services() as s:
            return _candidate_json(s.store.edit_value(candidate_id, actor, body.value))

    @app.post("/candidates/{candidate_id}/item")
    def edit_item(candidate_id: CandidateId, body: ItemIn, actor: Actor) -> Any:
        with open_services() as s:
            return _candidate_json(
                s.store.edit_item(candidate_id, actor, product_id=body.product_id,
                                  quantity=body.quantity)
            )

    return app
