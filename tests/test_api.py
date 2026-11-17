"""Tests for the web API.

Every test uses the fake model and a temporary database: no key, no network,
no paid call. The app is built with create_app() directly, never from .env.
"""

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fieldnote import main
from fieldnote.api import BAD_REQUEST_SHAPE, EXTRACTION_FAILED, MAX_ID, USERS, create_app
from fieldnote.audit import AuditLog
from fieldnote.candidates import CandidateStore
from fieldnote.catalog import Catalog
from fieldnote.extraction import MAX_NOTE_CHARS
from fieldnote.llm import FakeProvider, ModelError
from fieldnote.policy import PolicyEngine, Rule, Tier
from fieldnote.policy_table import POLICY_TABLE

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.from_csv(ROOT / "data" / "catalog.csv")

REP = {"X-Acting-As": "sales_rep"}
OWNER = {"X-Acting-As": "owner"}

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
NAMES = ["account", "contact", "email", "phone", "subject", "due", "arms", "gripper", "promise"]
MONEY_WORDS = ("price", "total", "discount", "tax", "amount")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


@pytest.fixture
def provider():
    return FakeProvider(ANSWER)


def make_client(db_path, provider, table=POLICY_TABLE):
    return TestClient(create_app(db_path=db_path, provider=provider, catalog=CATALOG, table=table))


@pytest.fixture
def client(db_path, provider):
    return make_client(db_path, provider)


@pytest.fixture
def saved(client):
    """Submit the worked example as the sales rep. Its candidates, by a short name."""
    response = client.post("/notes", json={"text": NOTE}, headers=REP)
    assert response.status_code == 201
    return dict(zip(NAMES, response.json()["candidates"], strict=True))


def audit_log(db_path):
    with AuditLog(db_path) as log:
        assert log.verify().ok
        return [(e.actor, e.action_type, e.outcome) for e in log.events()]


def note_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    except sqlite3.OperationalError:  # the table was never created
        return 0
    finally:
        conn.close()


def candidates_now(client, note_id=1):
    return client.get(f"/notes/{note_id}", headers=REP).json()["candidates"]


def all_keys(value):
    """Every key anywhere inside a JSON answer."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


# --- Who is acting ----------------------------------------------------------------

# (method, path as the app declares it, a real path, a valid body)
ROUTES = [
    ("GET", "/products", "/products", None),
    ("POST", "/notes", "/notes", {"text": NOTE}),
    ("GET", "/notes/{note_id}", "/notes/1", None),
    ("POST", "/notes/{note_id}/extract", "/notes/1/extract", None),
    ("POST", "/notes/{note_id}/confirm-all", "/notes/1/confirm-all", None),
    ("POST", "/candidates/{candidate_id}/confirm", "/candidates/1/confirm", None),
    ("POST", "/candidates/{candidate_id}/reject", "/candidates/1/reject", None),
    ("POST", "/candidates/{candidate_id}/value", "/candidates/1/value", {"value": "Changed Inc"}),
    ("POST", "/candidates/{candidate_id}/item", "/candidates/7/item", {"quantity": 5}),
]
WITH_ID = [route for route in ROUTES if "{" in route[1]]
CHANGES = [route for route in WITH_ID if route[1] != "/notes/{note_id}"]
# Routes that need no user: FastAPI's own help pages, and the three files of
# the phone page (the page holds no data; tests/test_page.py covers it).
BUILT_IN = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/", "/app.js", "/app.css"}


def call(client, route, headers):
    method, _, path, body = route
    return client.request(method, path, json=body, headers=headers)


def test_the_route_list_above_is_complete(client):
    """A route added to the app without being added to ROUTES fails here, so
    it cannot skip the who-is-acting test below."""
    declared = {
        (method, route.path)
        for route in client.app.routes
        if route.path not in BUILT_IN
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    assert declared == {(method, path) for method, path, _, _ in ROUTES}


def test_the_users_are_fixed():
    assert USERS == ("sales_rep", "owner")


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Acting-As": ""}, {"X-Acting-As": "admin"}, {"X-Acting-As": "Sales_Rep"},
     {"X-Acting-As": "sales_rep,owner"}, {"X-Acting-As": "sales_rep\x00"}],
    ids=["missing", "empty", "unknown", "wrong_case", "two_names", "trailing_null"],
)
def test_every_route_refuses_an_unknown_user(client, saved, db_path, provider, route, headers):
    before, log_before = candidates_now(client), audit_log(db_path)

    response = call(client, route, headers)

    assert response.status_code == 401
    assert "X-Acting-As" in response.json()["detail"]
    assert candidates_now(client) == before
    assert audit_log(db_path) == log_before
    assert note_count(db_path) == 1 and len(provider.calls) == 1  # only the fixture's


def test_an_unknown_user_is_refused_before_the_body_is_looked_at(client, db_path):
    response = client.post("/notes", json={"wrong": "shape"}, headers={})
    assert response.status_code == 401
    assert note_count(db_path) == 0


# --- Submitting a note ---------------------------------------------------------------


def test_submit_saves_the_note_and_returns_the_suggestions(client, db_path, provider):
    response = client.post("/notes", json={"text": NOTE}, headers=REP)

    assert response.status_code == 201
    body = response.json()
    created_at = body["note"].pop("created_at")
    assert isinstance(created_at, str) and created_at.endswith("+00:00")
    assert body["note"] == {"id": 1, "author": "sales_rep", "text": NOTE, "locked": False}
    assert [(c["type"], c["name"], c["state"]) for c in body["candidates"]] == [
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
    start = NOTE.index("Bayside Packaging")
    # The whole shape of one suggestion, key by key.
    assert body["candidates"][0] == {
        "id": 1,
        "note_id": 1,
        "type": "field",
        "name": "account.name",
        "state": "pending",
        "value": "Bayside Packaging",
        "product_id": None,
        "quantity": None,
        "kind": "observed",
        "edited": False,
        "span": [start, start + len("Bayside Packaging")],
        "needs_individual_confirmation": False,
        "original": {
            "value": "Bayside Packaging",
            "product_id": None,
            "quantity": None,
            "confidence": "high",
            "reasons": [],
        },
        "decided_by": None,
        "decided_at": None,
    }
    arms = body["candidates"][6]
    assert (arms["product_id"], arms["quantity"], arms["value"]) == ("ARM-6KG", 2, None)
    assert NOTE[arms["span"][0] : arms["span"][1]] == "two 6 kg arms"

    assert audit_log(db_path) == [
        ("sales_rep", "note.submit", "allowed"),
        ("sales_rep", "note.submit", "succeeded"),
        ("sales_rep", "extraction.run", "allowed"),
        ("sales_rep", "extraction.run", "succeeded"),
    ]
    (sent,) = provider.calls
    assert NOTE in sent.user


def test_the_author_is_whoever_is_acting(client):
    body = client.post("/notes", json={"text": NOTE}, headers=OWNER).json()
    assert body["note"]["author"] == "owner"


def test_reading_a_note_gives_the_same_answer_as_submitting_it(client):
    submitted = client.post("/notes", json={"text": NOTE}, headers=REP).json()
    response = client.get("/notes/1", headers=REP)
    assert response.status_code == 200
    assert response.json() == submitted


@pytest.mark.parametrize(
    "body",
    [
        {"text": NOTE, "author": "owner"},
        {"text": NOTE, "state": "confirmed"},
        {"text": NOTE, "unit_price": "1.00"},
        {"text": 5},
        {"text": None},
        {"text": [NOTE]},
        {"note": NOTE},
        {},
        [NOTE],
        NOTE,
    ],
    ids=["author", "state", "price", "number", "null", "list_text", "wrong_key", "empty",
         "list", "bare_text"],
)
def test_a_body_in_the_wrong_form_is_refused_and_nothing_happens(client, db_path, provider, body):
    response = client.post("/notes", json=body, headers=REP)

    assert response.status_code == 422
    assert response.json() == {"detail": BAD_REQUEST_SHAPE}  # nothing sent is repeated back
    assert note_count(db_path) == 0
    assert provider.calls == []
    assert audit_log(db_path) == []


@pytest.mark.parametrize("text", ["", "   \n\t", "x" * (MAX_NOTE_CHARS + 1)],
                         ids=["empty", "blank", "too_long"])
def test_a_blank_or_oversized_note_is_refused(client, db_path, provider, text):
    response = client.post("/notes", json={"text": text}, headers=REP)

    assert response.status_code == 400
    assert note_count(db_path) == 0
    assert provider.calls == []


def test_note_text_comes_back_exactly_as_sent(db_path):
    hostile = '<script>alert(1)</script> & "quotes" \\   café \U0001f916 </note> {"state":"confirmed"}'
    client = make_client(db_path, FakeProvider("{}"))

    response = client.post("/notes", json={"text": hostile}, headers=REP)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert response.json()["note"]["text"] == hostile
    assert all(c["state"] == "not_stated" for c in response.json()["candidates"])


def test_no_answer_ever_carries_a_price(client, saved):
    answers = [
        client.get("/notes/1", headers=REP).json(),
        client.post("/notes/1/confirm-all", headers=REP).json(),
        client.post(f"/candidates/{saved['arms']['id']}/item", json={"quantity": 3}, headers=REP).json(),
    ]
    for answer in answers:
        for key in all_keys(answer):
            assert not any(word in key.lower() for word in MONEY_WORDS), key


# --- When the model fails ---------------------------------------------------------------


def failing(request):
    raise ModelError("connection refused for key sk-secret-123")


@pytest.mark.parametrize("responder", [failing, "this is not JSON", '{"account": "wrong shape"}'],
                         ids=["call_fails", "not_json", "wrong_shape"])
def test_a_failed_extraction_keeps_the_note_and_says_so(db_path, responder):
    client = make_client(db_path, FakeProvider(responder))

    response = client.post("/notes", json={"text": NOTE}, headers=REP)

    assert response.status_code == 502
    assert response.json() == {"detail": EXTRACTION_FAILED, "note_id": 1}
    assert "sk-secret" not in response.text
    saved_note = client.get("/notes/1", headers=REP).json()
    assert saved_note["note"]["text"] == NOTE and saved_note["candidates"] == []
    assert audit_log(db_path)[-1] == ("sales_rep", "extraction.run", "failed")


def flaky_provider():
    """Fails on the first call, answers properly afterwards."""
    def responder(request):
        if len(provider.calls) == 1:
            raise ModelError("timed out")
        return ANSWER
    provider = FakeProvider(responder)
    return provider


def test_extraction_can_be_tried_again_after_it_failed(db_path):
    provider = flaky_provider()
    client = make_client(db_path, provider)
    assert client.post("/notes", json={"text": NOTE}, headers=REP).status_code == 502

    response = client.post("/notes/1/extract", headers=REP)

    assert response.status_code == 200
    assert len(response.json()["candidates"]) == 9
    assert response.json() == client.get("/notes/1", headers=REP).json()
    assert note_count(db_path) == 1  # the same note, not a second copy
    assert audit_log(db_path)[-2:] == [
        ("sales_rep", "extraction.run", "allowed"),
        ("sales_rep", "extraction.run", "succeeded"),
    ]


def test_extraction_is_not_run_twice_on_a_note(client, saved, provider):
    response = client.post("/notes/1/extract", headers=REP)

    assert response.status_code == 400
    assert "already saved" in response.json()["detail"]
    assert len(provider.calls) == 1  # the model was not asked again
    assert candidates_now(client) == list(saved.values())


def test_only_the_author_may_try_the_extraction_again(db_path):
    provider = flaky_provider()
    client = make_client(db_path, provider)
    client.post("/notes", json={"text": NOTE}, headers=REP)

    response = client.post("/notes/1/extract", headers=OWNER)

    assert response.status_code == 403
    assert len(provider.calls) == 1
    assert audit_log(db_path)[-1] == ("owner", "extraction.run", "refused")


# --- Reading --------------------------------------------------------------------------


def test_either_user_may_read_a_note(client, saved):
    as_rep = client.get("/notes/1", headers=REP)
    as_owner = client.get("/notes/1", headers=OWNER)
    assert as_owner.status_code == 200
    assert as_owner.json() == as_rep.json()


@pytest.mark.parametrize("route", WITH_ID, ids=lambda r: f"{r[0]} {r[1]}")
def test_an_unknown_id_is_not_found(client, saved, provider, route):
    method, _, path, body = route
    missing = path.replace("/1", "/999").replace("/7", "/999")
    response = client.request(method, missing, json=body, headers=REP)
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    assert len(provider.calls) == 1


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "1.5", str(MAX_ID + 1), str(10**30)])
def test_an_id_that_is_not_a_positive_whole_number_is_refused(client, saved, bad_id):
    assert client.get(f"/notes/{bad_id}", headers=REP).status_code == 422
    assert client.post(f"/candidates/{bad_id}/confirm", headers=REP).status_code == 422
    assert candidates_now(client) == list(saved.values())


# --- Confirm and reject ---------------------------------------------------------------------


def test_confirm_one(client, saved, db_path):
    response = client.post(f"/candidates/{saved['account']['id']}/confirm", headers=REP)

    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["decided_by"]) == ("confirmed", "sales_rep")
    assert body["decided_at"] is not None
    assert candidates_now(client)[0] == body
    assert [c["state"] for c in candidates_now(client)[1:]] == [c["state"] for c in list(saved.values())[1:]]
    assert audit_log(db_path)[-1] == ("sales_rep", "candidate.confirm", "allowed")


def test_reject_one(client, saved, db_path):
    response = client.post(f"/candidates/{saved['promise']['id']}/reject", headers=REP)

    assert response.status_code == 200
    assert (response.json()["state"], response.json()["decided_by"]) == ("rejected", "sales_rep")
    assert candidates_now(client)[8]["state"] == "rejected"
    assert audit_log(db_path)[-1] == ("sales_rep", "candidate.reject", "allowed")


@pytest.mark.parametrize(
    "name, action, words",
    [
        ("email", "confirm", "nothing to confirm"),
        ("email", "reject", "nothing to reject"),
        ("gripper", "confirm", "pick a product"),
    ],
)
def test_a_confirm_or_reject_that_breaks_a_rule_is_refused(client, saved, name, action, words):
    response = client.post(f"/candidates/{saved[name]['id']}/{action}", headers=REP)

    assert response.status_code == 400
    assert words in response.json()["detail"]
    assert candidates_now(client) == list(saved.values())


# --- Edits -------------------------------------------------------------------------------


def test_edit_a_value_then_confirm_it_as_a_second_step(client, saved):
    account = saved["account"]["id"]

    edited = client.post(f"/candidates/{account}/value", json={"value": "Bayside Packaging LLC"},
                         headers=REP)

    assert edited.status_code == 200
    body = edited.json()
    assert (body["value"], body["state"], body["edited"]) == ("Bayside Packaging LLC", "pending", True)
    assert body["span"] is None  # the words no longer come from the note
    assert body["original"]["value"] == "Bayside Packaging"
    assert body["needs_individual_confirmation"] is True
    assert body["decided_by"] is None

    # Confirm-all skips it: an edited value needs its own tap.
    bulk = client.post("/notes/1/confirm-all", headers=REP).json()
    assert account not in bulk["confirmed"]
    assert account in [c["id"] for c in bulk["waiting"]]
    assert candidates_now(client)[0]["state"] == "pending"

    confirmed = client.post(f"/candidates/{account}/confirm", headers=REP).json()
    assert (confirmed["state"], confirmed["value"]) == ("confirmed", "Bayside Packaging LLC")


def test_fill_in_a_field_the_note_did_not_state(client, saved):
    body = client.post(f"/candidates/{saved['email']['id']}/value",
                       json={"value": "dana@bayside.example"}, headers=REP).json()
    assert (body["state"], body["value"], body["kind"]) == ("pending", "dana@bayside.example", "observed")
    assert body["original"]["value"] is None


@pytest.mark.parametrize(
    "body, status",
    [
        ({"value": ""}, 400),
        ({"value": "   "}, 400),
        ({"value": "x" * 501}, 400),
        ({"value": 5}, 422),
        ({"value": None}, 422),
        ({"value": ["Changed"]}, 422),
        ({}, 422),
        ({"value": "Changed", "state": "confirmed"}, 422),
        ({"value": "Changed", "kind": "customer_stated"}, 422),
        ({"value": "Changed", "edited": False}, 422),
    ],
)
def test_a_bad_value_edit_changes_nothing(client, saved, body, status):
    response = client.post(f"/candidates/{saved['account']['id']}/value", json=body, headers=REP)
    assert response.status_code == status
    assert candidates_now(client) == list(saved.values())


def test_edit_an_item_then_confirm_it(client, saved):
    gripper = saved["gripper"]["id"]

    edited = client.post(f"/candidates/{gripper}/item", json={"product_id": "GRIP-2F-85"},
                         headers=REP)

    assert edited.status_code == 200
    body = edited.json()
    assert (body["product_id"], body["quantity"], body["state"], body["edited"]) == (
        "GRIP-2F-85", 1, "pending", True)
    assert body["original"]["product_id"] is None

    both = client.post(f"/candidates/{gripper}/item", json={"product_id": "GRIP-VAC-4", "quantity": 4},
                       headers=REP).json()
    assert (both["product_id"], both["quantity"]) == ("GRIP-VAC-4", 4)

    assert client.post(f"/candidates/{gripper}/confirm", headers=REP).json()["state"] == "confirmed"


@pytest.mark.parametrize(
    "body, status",
    [
        ({}, 400),
        ({"product_id": "NOT-A-PRODUCT"}, 400),
        ({"product_id": "arm-6kg"}, 400),
        ({"product_id": ""}, 400),
        ({"quantity": 0}, 400),
        ({"quantity": -2}, 400),
        ({"quantity": 10**9}, 400),
        ({"quantity": "2"}, 422),
        ({"quantity": True}, 422),
        ({"quantity": 2.5}, 422),
        ({"product_id": 7}, 422),
        ({"quantity": 2, "unit_price": "1.00"}, 422),
        ({"quantity": 2, "discount": 50}, 422),
        ({"product_id": "ARM-6KG", "total": 1}, 422),
        ({"quantity": 2, "state": "confirmed"}, 422),
    ],
)
def test_a_bad_item_edit_changes_nothing(client, saved, body, status):
    response = client.post(f"/candidates/{saved['arms']['id']}/item", json=body, headers=REP)
    assert response.status_code == status
    assert candidates_now(client) == list(saved.values())


def test_the_two_kinds_of_edit_do_not_mix(client, saved):
    on_item = client.post(f"/candidates/{saved['arms']['id']}/value", json={"value": "x"}, headers=REP)
    on_field = client.post(f"/candidates/{saved['account']['id']}/item", json={"quantity": 2},
                           headers=REP)
    assert (on_item.status_code, on_field.status_code) == (400, 400)
    assert candidates_now(client) == list(saved.values())


# --- Confirm all ------------------------------------------------------------------------------


def test_confirm_all_confirms_the_plain_ones_and_lists_the_rest(client, saved):
    response = client.post("/notes/1/confirm-all", headers=REP)

    assert response.status_code == 200
    body = response.json()
    assert body["confirmed"] == [saved[n]["id"] for n in ("account", "contact", "subject", "due")]
    # Low-confidence items and the promise each need their own tap.
    assert [c["id"] for c in body["waiting"]] == [saved[n]["id"] for n in ("arms", "gripper", "promise")]
    assert all(c["state"] == "pending" for c in body["waiting"])
    assert [c["state"] for c in candidates_now(client)] == [
        "confirmed", "confirmed", "not_stated", "not_stated", "confirmed", "confirmed",
        "pending", "pending", "pending",
    ]

    again = client.post("/notes/1/confirm-all", headers=REP).json()
    assert again["confirmed"] == []
    assert [c["id"] for c in again["waiting"]] == [c["id"] for c in body["waiting"]]


# --- Only the author reviews ---------------------------------------------------------------------


@pytest.mark.parametrize("route", CHANGES, ids=lambda r: f"{r[0]} {r[1]}")
def test_someone_else_cannot_change_the_note(client, saved, db_path, provider, route):
    response = call(client, route, OWNER)

    assert response.status_code == 403
    assert response.json() == {"detail": "only the note's author may review it"}
    assert candidates_now(client) == list(saved.values())
    actor, _, outcome = audit_log(db_path)[-1]
    assert (actor, outcome) == ("owner", "refused")
    assert len(provider.calls) == 1


def test_the_owner_reviews_his_own_note_but_not_the_reps(client, saved):
    mine = client.post("/notes", json={"text": NOTE}, headers=OWNER).json()["candidates"][0]["id"]
    assert client.post(f"/candidates/{mine}/confirm", headers=OWNER).json()["state"] == "confirmed"
    assert client.post(f"/candidates/{mine}/confirm", headers=REP).status_code == 403


# --- Locked notes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("route", CHANGES, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_locked_note_cannot_change(client, saved, db_path, provider, route):
    with AuditLog(db_path) as audit, CandidateStore(
        db_path, policy=PolicyEngine(audit, POLICY_TABLE), audit=audit,
        product_names=CATALOG.product_names(),
    ) as store:
        store.lock_note(1, "sales_rep")

    response = call(client, route, REP)

    assert response.status_code == 409
    assert "locked" in response.json()["detail"]
    after = client.get("/notes/1", headers=REP).json()
    assert after["note"]["locked"] is True
    assert after["candidates"] == list(saved.values())
    assert len(provider.calls) == 1


# --- Policy ---------------------------------------------------------------------------------------------


def test_a_policy_refusal_reaches_the_caller_and_changes_nothing(db_path, provider):
    table = {name: rule for name, rule in POLICY_TABLE.items() if name != "candidate.confirm"}
    client = make_client(db_path, provider, table)
    client.post("/notes", json={"text": NOTE}, headers=REP)

    one = client.post("/candidates/1/confirm", headers=REP)
    bulk = client.post("/notes/1/confirm-all", headers=REP)

    for response in (one, bulk):
        assert response.status_code == 403
        assert response.json() == {"detail": "refused by policy: action type not in policy table"}
    assert all(c["state"] != "confirmed" for c in candidates_now(client))
    assert audit_log(db_path)[-1] == ("sales_rep", "candidate.confirm", "refused")


def test_a_refused_note_is_not_saved_and_the_model_is_not_asked(db_path, provider):
    client = make_client(db_path, provider, {"extraction.run": Rule(Tier.GREEN)})

    response = client.post("/notes", json={"text": NOTE}, headers=REP)

    assert response.status_code == 403
    assert note_count(db_path) == 0
    assert provider.calls == []


def test_a_refused_extraction_does_not_ask_the_model(db_path, provider):
    client = make_client(db_path, provider, {"note.submit": Rule(Tier.GREEN)})

    response = client.post("/notes", json={"text": NOTE}, headers=REP)

    assert response.status_code == 403
    assert provider.calls == []
    assert audit_log(db_path)[-1] == ("sales_rep", "extraction.run", "refused")


# --- One request at a time ----------------------------------------------------------------------------


def test_requests_take_turns(db_path):
    inside = {"now": 0, "most": 0}

    def slow(request):
        inside["now"] += 1
        inside["most"] = max(inside["most"], inside["now"])
        time.sleep(0.2)
        inside["now"] -= 1
        return ANSWER

    app = create_app(db_path=db_path, provider=FakeProvider(slow), catalog=CATALOG)
    start = threading.Barrier(2)
    statuses = []

    def submit():
        start.wait()
        statuses.append(TestClient(app).post("/notes", json={"text": NOTE}, headers=REP).status_code)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [201, 201]
    assert inside["most"] == 1  # never two requests inside the model call together
    assert note_count(db_path) == 2
    assert len(audit_log(db_path)) == 8


# --- Starting the app from settings ----------------------------------------------------------------------


def test_build_app_uses_the_database_and_provider_from_the_settings(tmp_path):
    settings = {"LLM_PROVIDER": "fake", "DATABASE_PATH": str(tmp_path / "chosen.db")}
    client = TestClient(main.build_app(settings, ROOT))

    response = client.post("/notes", json={"text": NOTE}, headers=REP)

    assert response.status_code == 201
    # The built-in fake answers with nothing, so nothing is stated.
    assert {c["state"] for c in response.json()["candidates"]} == {"not_stated"}
    assert note_count(tmp_path / "chosen.db") == 1


def test_a_misspelled_provider_stops_the_app_from_starting(tmp_path):
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        main.build_app({"LLM_PROVIDER": "cluade", "DATABASE_PATH": str(tmp_path / "x.db")}, ROOT)


def test_app_reads_the_env_file_in_its_root_folder(tmp_path, monkeypatch):
    # A stand-in project folder, so the real .env is never read.
    (tmp_path / "data").mkdir()
    shutil.copy(ROOT / "data" / "catalog.csv", tmp_path / "data" / "catalog.csv")
    (tmp_path / ".env").write_text("LLM_PROVIDER=fake\n", encoding="utf-8")
    monkeypatch.setattr(main, "ROOT", tmp_path)
    for name in ("LLM_PROVIDER", "LLM_MODEL", "ANTHROPIC_API_KEY", "DATABASE_PATH"):
        monkeypatch.delenv(name, raising=False)

    client = TestClient(main.app())

    assert client.post("/notes", json={"text": NOTE}, headers=REP).status_code == 201
    assert note_count(tmp_path / "fieldnote.db") == 1
