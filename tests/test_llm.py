"""Tests for the model adapter and the fake provider."""

import pytest

from fieldnote.llm import (
    FakeProvider,
    ModelError,
    ModelRequest,
    ModelResponse,
    get_provider,
)

REQUEST = ModelRequest(system="Extract the records.", user="Visited Acme today.")


# --- Requests -------------------------------------------------------------------


@pytest.mark.parametrize("field", ["system", "user"])
@pytest.mark.parametrize("value", ["", "   ", None, 5])
def test_blank_or_non_text_request_part_is_refused(field, value):
    parts = {"system": "Extract the records.", "user": "Visited Acme today."}
    parts[field] = value
    with pytest.raises(ValueError, match=field):
        ModelRequest(**parts)


def test_request_cannot_be_changed_after_creation():
    with pytest.raises(AttributeError):
        REQUEST.user = "something else"


# --- Fake provider ----------------------------------------------------------------


def test_fake_returns_the_scripted_reply():
    response = FakeProvider('{"account": "Acme"}').complete(REQUEST)
    assert response == ModelResponse(text='{"account": "Acme"}', provider="fake", model="fake")


def test_fake_can_build_its_reply_from_the_request():
    fake = FakeProvider(lambda request: request.user.upper())
    assert fake.complete(REQUEST).text == "VISITED ACME TODAY."


def test_fake_remembers_every_request_in_order():
    fake = FakeProvider("ok")
    second = ModelRequest(system="Extract the records.", user="Second note.")
    fake.complete(REQUEST)
    fake.complete(second)
    assert fake.calls == [REQUEST, second]


def test_fake_remembers_a_request_even_when_the_reply_fails():
    def failing(request):
        raise ModelError("model unavailable")

    fake = FakeProvider(failing)
    with pytest.raises(ModelError):
        fake.complete(REQUEST)
    assert fake.calls == [REQUEST]


def test_fake_refuses_anything_that_is_not_a_request():
    fake = FakeProvider("ok")
    with pytest.raises(TypeError):
        fake.complete("Visited Acme today.")
    assert fake.calls == []


def test_fake_responder_must_return_text():
    with pytest.raises(TypeError, match="string"):
        FakeProvider(lambda request: {"account": "Acme"}).complete(REQUEST)


def test_fake_responder_must_be_text_or_a_function():
    with pytest.raises(TypeError):
        FakeProvider(42)


# --- Choosing a provider ------------------------------------------------------------


def test_default_provider_is_the_fake_and_needs_no_key():
    assert isinstance(get_provider({}), FakeProvider)


def test_fake_can_be_chosen_by_name():
    assert isinstance(get_provider({"LLM_PROVIDER": "fake"}), FakeProvider)


@pytest.mark.parametrize("name", ["gpt", "FAKE", " fake", "fake ", ""])
def test_unknown_provider_name_is_refused(name):
    with pytest.raises(ValueError, match="unknown LLM_PROVIDER"):
        get_provider({"LLM_PROVIDER": name})


def test_error_for_unknown_provider_does_not_reveal_the_key():
    env = {"LLM_PROVIDER": "gpt", "LLM_API_KEY": "sk-test-do-not-print"}
    with pytest.raises(ValueError) as error:
        get_provider(env)
    assert "sk-test-do-not-print" not in str(error.value)
