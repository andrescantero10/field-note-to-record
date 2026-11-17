"""Tests for the CRM adapter and its mock.

The mock is a pretend Zoho kept in a temporary database. Nothing here uses the
network or a Zoho account.
"""

import dataclasses
import inspect
import sqlite3
from datetime import date, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fieldnote import crm, main
from fieldnote.crm import (
    Account,
    Contact,
    CrmClient,
    CrmError,
    Deal,
    MockCrm,
    Task,
    crm_mode,
    get_crm,
)

MONEY_WORDS = ("price", "total", "discount", "tax", "amount", "invoice", "quote")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "app.db"


@pytest.fixture
def mock(db_path):
    with MockCrm(db_path) as m:
        yield m


def fields_of(mock, module):
    return [record.fields for record in mock.records(module)]


# --- What the adapter can do, and what it cannot ---------------------------------------


def public_methods(cls):
    return {name for name, value in inspect.getmembers(cls, callable) if not name.startswith("_")}


def test_the_client_has_exactly_four_actions():
    assert public_methods(CrmClient) == {"save_account", "save_contact", "save_deal", "create_task"}


def test_the_mock_adds_only_ways_to_look_at_what_it_holds():
    extra = public_methods(MockCrm) - public_methods(CrmClient)
    assert extra == {"records", "get", "close"}
    assert isinstance(MockCrm(":memory:"), CrmClient)


def test_nothing_in_the_module_can_quote_invoice_send_or_delete():
    forbidden = ("quote", "invoice", "send", "email_customer", "delete", "remove", "books")
    for name in dir(crm):
        if name.startswith("__"):
            continue
        assert not any(word in name.lower() for word in forbidden), name
    for cls in (CrmClient, MockCrm):
        for name in public_methods(cls):
            assert not any(word in name.lower() for word in forbidden), name


@pytest.mark.parametrize("record_type", [Account, Contact, Deal, Task])
def test_no_record_has_a_money_field(record_type):
    for field in dataclasses.fields(record_type):
        assert not any(word in field.name.lower() for word in MONEY_WORDS), field.name


def test_record_fields_are_exactly_these():
    names = lambda cls: [f.name for f in dataclasses.fields(cls)]
    assert names(Account) == ["name"]
    assert names(Contact) == ["name", "email", "phone", "account_id"]
    assert names(Deal) == ["name", "account_id", "contact_id"]
    assert names(Task) == ["subject", "due_date", "deal_id", "contact_id"]


# --- Records check themselves ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", None, 5, ["Bayside"]])
def test_a_record_needs_a_real_name(bad):
    for build in (
        lambda: Account(bad),
        lambda: Contact(bad),
        lambda: Deal(bad),
        lambda: Task(bad),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("bad", ["", "  ", 5, ["x"]])
def test_optional_text_is_text_or_missing(bad):
    for build in (
        lambda: Contact("Dana", email=bad),
        lambda: Contact("Dana", phone=bad),
        lambda: Contact("Dana", account_id=bad),
        lambda: Deal("Deal", account_id=bad),
        lambda: Deal("Deal", contact_id=bad),
        lambda: Task("Call", deal_id=bad),
        lambda: Task("Call", contact_id=bad),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("bad", ["2026-11-20", "Friday", 5, datetime(2026, 11, 20, 9, 0)])
def test_a_task_due_date_is_a_date_or_missing(bad):
    with pytest.raises(ValueError):
        Task("Call her", due_date=bad)
    assert Task("Call her", due_date=date(2026, 11, 20)).due_date == date(2026, 11, 20)
    assert Task("Call her").due_date is None


def test_records_cannot_be_changed_after_they_are_made():
    account = Account("Bayside Packaging")
    with pytest.raises(dataclasses.FrozenInstanceError):
        account.name = "Other"


# --- Accounts ---------------------------------------------------------------------------------


def test_save_an_account(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))

    assert isinstance(account_id, str) and account_id.startswith("mock-")
    record = mock.get(account_id)
    assert (record.id, record.module, record.fields) == (
        account_id, "Accounts", {"name": "Bayside Packaging"})
    assert mock.records("Accounts") == [record]


def test_saving_the_same_account_twice_makes_two_records_like_the_real_crm(mock):
    # The real CRM has no unique field for account names. The mock keeps that
    # flaw on purpose: preventing the duplicate is the writer's job (Task 11).
    first = mock.save_account(Account("Bayside Packaging"))
    second = mock.save_account(Account("Bayside Packaging"))
    assert first != second
    assert len(mock.records("Accounts")) == 2


def test_saving_with_an_existing_id_updates_that_record(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))

    again = mock.save_account(Account("Bayside Packaging LLC"), existing_id=account_id)

    assert again == account_id
    assert fields_of(mock, "Accounts") == [{"name": "Bayside Packaging LLC"}]


@pytest.mark.parametrize("bad_id", ["mock-999", "999", "mock-", "mock-1; DROP TABLE", "", 1, "MOCK-1"])
def test_an_unknown_existing_id_is_an_error_and_saves_nothing(mock, bad_id):
    mock.save_account(Account("Bayside Packaging"))
    with pytest.raises(CrmError):
        mock.save_account(Account("Changed"), existing_id=bad_id)
    assert fields_of(mock, "Accounts") == [{"name": "Bayside Packaging"}]


def test_an_id_from_another_kind_of_record_is_refused(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))
    contact_id = mock.save_contact(Contact("Dana"))

    with pytest.raises(CrmError):
        mock.save_account(Account("Changed"), existing_id=contact_id)
    with pytest.raises(CrmError):
        mock.save_contact(Contact("Changed"), existing_id=account_id)
    with pytest.raises(CrmError):
        mock.save_deal(Deal("Changed"), existing_id=account_id)

    assert fields_of(mock, "Accounts") == [{"name": "Bayside Packaging"}]
    assert [f["name"] for f in fields_of(mock, "Contacts")] == ["Dana"]


def test_each_action_takes_only_its_own_kind_of_record(mock):
    with pytest.raises(TypeError):
        mock.save_account(Contact("Dana"))
    with pytest.raises(TypeError):
        mock.save_contact(Account("Bayside"))
    with pytest.raises(TypeError):
        mock.save_deal({"name": "Deal"})
    with pytest.raises(TypeError):
        mock.create_task(Deal("Deal"))
    for module in ("Accounts", "Contacts", "Deals", "Tasks"):
        assert mock.records(module) == []


# --- Contacts -----------------------------------------------------------------------------------


def test_save_a_contact_linked_to_an_account(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))

    contact_id = mock.save_contact(
        Contact("Dana", email="dana@bayside.example", phone="555-0100", account_id=account_id))

    assert mock.get(contact_id).fields == {
        "name": "Dana", "email": "dana@bayside.example", "phone": "555-0100",
        "account_id": account_id,
    }


def test_the_same_email_is_the_same_contact(mock):
    first = mock.save_contact(Contact("Dana", email="dana@bayside.example"))

    second = mock.save_contact(Contact("Dana Reyes", email="dana@bayside.example", phone="555-0100"))

    assert second == first
    assert fields_of(mock, "Contacts") == [
        {"name": "Dana Reyes", "email": "dana@bayside.example", "phone": "555-0100",
         "account_id": None}
    ]


def test_email_matching_ignores_upper_and_lower_case(mock):
    first = mock.save_contact(Contact("Dana", email="Dana@Bayside.example"))
    second = mock.save_contact(Contact("Dana", email="dana@bayside.EXAMPLE"))
    assert second == first
    assert len(mock.records("Contacts")) == 1


def test_contacts_without_an_email_are_never_merged(mock):
    first = mock.save_contact(Contact("Dana"))
    second = mock.save_contact(Contact("Dana"))
    assert first != second
    assert len(mock.records("Contacts")) == 2


def test_a_contact_can_be_updated_by_id_including_its_email(mock):
    contact_id = mock.save_contact(Contact("Dana", email="dana@bayside.example"))

    mock.save_contact(Contact("Dana", email="dana.r@bayside.example"), existing_id=contact_id)

    assert [f["email"] for f in fields_of(mock, "Contacts")] == ["dana.r@bayside.example"]
    # The old email is free again.
    other = mock.save_contact(Contact("Someone else", email="dana@bayside.example"))
    assert other != contact_id


def test_a_contact_cannot_take_an_email_that_belongs_to_another(mock):
    dana = mock.save_contact(Contact("Dana", email="dana@bayside.example"))
    sam = mock.save_contact(Contact("Sam", email="sam@bayside.example"))

    with pytest.raises(CrmError, match="email"):
        mock.save_contact(Contact("Sam", email="DANA@bayside.example"), existing_id=sam)

    assert mock.get(sam).fields["email"] == "sam@bayside.example"
    assert mock.get(dana).fields["name"] == "Dana"


def test_the_database_itself_refuses_two_contacts_with_one_email(mock, db_path):
    mock.save_contact(Contact("Dana", email="dana@bayside.example"))
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mock_crm_records (module, email_key, fields)"
            " VALUES ('Contacts', 'dana@bayside.example', '{}')"
        )
    conn.close()


# --- Deals and tasks --------------------------------------------------------------------------------


def test_save_a_deal_linked_to_its_account_and_contact(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))
    contact_id = mock.save_contact(Contact("Dana"))

    deal_id = mock.save_deal(Deal("Bayside Packaging: arms", account_id=account_id,
                                  contact_id=contact_id))

    assert mock.get(deal_id).module == "Deals"
    assert mock.get(deal_id).fields == {
        "name": "Bayside Packaging: arms", "account_id": account_id, "contact_id": contact_id}

    # Like accounts: no unique field, so a repeat without an id is a second record.
    mock.save_deal(Deal("Bayside Packaging: arms"))
    assert len(mock.records("Deals")) == 2
    mock.save_deal(Deal("Renamed"), existing_id=deal_id)
    assert mock.get(deal_id).fields["name"] == "Renamed"
    assert len(mock.records("Deals")) == 2


def test_create_a_task(mock):
    contact_id = mock.save_contact(Contact("Dana"))
    deal_id = mock.save_deal(Deal("Bayside Packaging: arms"))

    task_id = mock.create_task(
        Task("Call her", due_date=date(2026, 11, 20), deal_id=deal_id, contact_id=contact_id))

    assert mock.get(task_id).module == "Tasks"
    assert mock.get(task_id).fields == {
        "subject": "Call her", "due_date": "2026-11-20", "deal_id": deal_id,
        "contact_id": contact_id,
    }
    assert mock.get(mock.create_task(Task("No date"))).fields["due_date"] is None


def test_a_link_must_point_at_a_real_record_of_the_right_kind(mock):
    account_id = mock.save_account(Account("Bayside Packaging"))
    contact_id = mock.save_contact(Contact("Dana"))
    deal_id = mock.save_deal(Deal("Deal"))

    for attempt in (
        lambda: mock.save_contact(Contact("Sam", account_id="mock-999")),
        lambda: mock.save_contact(Contact("Sam", account_id=contact_id)),
        lambda: mock.save_deal(Deal("Other", account_id=deal_id)),
        lambda: mock.save_deal(Deal("Other", contact_id=account_id)),
        lambda: mock.create_task(Task("Call", deal_id=account_id)),
        lambda: mock.create_task(Task("Call", contact_id="not-an-id")),
    ):
        with pytest.raises(CrmError):
            attempt()

    assert len(mock.records("Contacts")) == 1
    assert len(mock.records("Deals")) == 1
    assert mock.records("Tasks") == []


def test_get_and_records_refuse_what_does_not_exist(mock):
    with pytest.raises(CrmError):
        mock.get("mock-1")
    with pytest.raises(ValueError):
        mock.records("Quotes")
    with pytest.raises(ValueError):
        mock.records("Invoices")


# --- Failing on purpose, call record, and what survives a restart ------------------------------------


def test_the_mock_can_be_told_to_fail_and_then_recovers(mock):
    mock.fail_next = 2

    with pytest.raises(CrmError):
        mock.save_account(Account("Bayside Packaging"))
    with pytest.raises(CrmError):
        mock.create_task(Task("Call"))
    assert mock.records("Accounts") == [] and mock.records("Tasks") == []

    account_id = mock.save_account(Account("Bayside Packaging"))  # works again
    assert mock.get(account_id).fields == {"name": "Bayside Packaging"}
    assert mock.fail_next == 0


def test_every_call_is_remembered_even_a_failed_one(mock):
    mock.save_account(Account("Bayside Packaging"))
    mock.fail_next = 1
    with pytest.raises(CrmError):
        mock.save_contact(Contact("Dana"))
    mock.save_deal(Deal("Deal"))
    mock.create_task(Task("Call"))
    assert mock.calls == ["save_account", "save_contact", "save_deal", "create_task"]


def test_records_survive_closing_and_ids_are_never_reused(db_path):
    with MockCrm(db_path) as first:
        account_id = first.save_account(Account("Bayside Packaging"))
        first.save_contact(Contact("Dana", email="dana@bayside.example"))

    with MockCrm(db_path) as second:
        assert second.get(account_id).fields == {"name": "Bayside Packaging"}
        new_id = second.save_account(Account("Northgate Foods"))
        assert new_id != account_id
        same = second.save_contact(Contact("Dana R", email="dana@bayside.example"))
        assert len(second.records("Contacts")) == 1 and second.get(same).fields["name"] == "Dana R"


def test_hostile_text_is_stored_as_text(mock, db_path):
    text = "Robert'); DROP TABLE mock_crm_records;-- <script>alert(1)</script>"
    account_id = mock.save_account(Account(text))
    assert mock.get(account_id).fields == {"name": text}
    mock.save_contact(Contact(text, email="x@example.com' OR '1'='1"))
    assert len(mock.records("Accounts")) == 1 and len(mock.records("Contacts")) == 1


@settings(max_examples=50, deadline=None)
@given(st.lists(st.sampled_from(["a@x.example", "A@X.example", "b@x.example", "B@x.EXAMPLE", None]),
                max_size=12))
def test_there_is_always_one_contact_per_email(emails):
    with MockCrm(":memory:") as mock:
        for email in emails:
            mock.save_contact(Contact("Someone", email=email))
        stored = [f["email"] for f in fields_of(mock, "Contacts")]
        with_email = [e.lower() for e in stored if e is not None]
        assert len(with_email) == len(set(with_email)) == len({e.lower() for e in emails if e})
        assert stored.count(None) == emails.count(None)


# --- Choosing the mode ------------------------------------------------------------------------------


def test_mock_is_the_default_mode(db_path):
    assert crm_mode({}) == "mock"
    assert crm_mode({"ZOHO_MODE": "mock"}) == "mock"
    client = get_crm({}, db_path)
    assert isinstance(client, MockCrm)
    client.close()


def test_live_mode_is_refused_until_it_is_built(db_path):
    for call in (lambda: crm_mode({"ZOHO_MODE": "live"}), lambda: get_crm({"ZOHO_MODE": "live"}, db_path)):
        with pytest.raises(NotImplementedError, match="not built yet"):
            call()


@pytest.mark.parametrize("mode", ["", "Mock", "MOCK", " mock", "fake", "production", "liev"])
def test_an_unknown_mode_is_an_error_never_a_silent_mock(db_path, mode):
    secrets = {"ZOHO_MODE": mode, "ZOHO_CLIENT_SECRET": "s3cret-value", "ZOHO_REFRESH_TOKEN": "tok-123"}
    for call in (lambda: crm_mode(secrets), lambda: get_crm(secrets, db_path)):
        with pytest.raises(ValueError, match="unknown ZOHO_MODE") as caught:
            call()
        assert "s3cret-value" not in str(caught.value) and "tok-123" not in str(caught.value)


@pytest.mark.parametrize("mode, error", [("live", NotImplementedError), ("liev", ValueError)])
def test_the_app_refuses_to_start_in_a_mode_it_cannot_run(tmp_path, mode, error):
    root = main.ROOT
    settings_ = {"LLM_PROVIDER": "fake", "DATABASE_PATH": str(tmp_path / "x.db"), "ZOHO_MODE": mode}
    with pytest.raises(error):
        main.build_app(settings_, root)


def test_the_app_starts_in_mock_mode(tmp_path):
    settings_ = {"LLM_PROVIDER": "fake", "DATABASE_PATH": str(tmp_path / "x.db"), "ZOHO_MODE": "mock"}
    assert main.build_app(settings_, main.ROOT) is not None
