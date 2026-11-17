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


# =============================================================================
# The Claude provider, tested with a stand-in for the SDK client: no key, no
# network. The stand-in records what would be sent and returns a canned reply.
# =============================================================================

from types import SimpleNamespace  # noqa: E402

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from fieldnote.llm import (  # noqa: E402
    DEFAULT_CLAUDE_MODEL,
    FALLBACK_BETA,
    MAX_OUTPUT_TOKENS,
    ClaudeProvider,
)

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
SECRET = "test-key-do-not-print"


def reply(text='{"ok": true}', stop_reason="end_turn", model="claude-opus-5-5", blocks=None):
    if blocks is None:
        blocks = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, model=model, content=blocks)


class StubClient:
    """Stands in for anthropic.Anthropic(): client.beta.messages.create(...)."""

    def __init__(self, result):
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._result = result

    def _create(self, **params):
        self.sent.append(params)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def claude(result, model=DEFAULT_CLAUDE_MODEL):
    client = StubClient(result)
    return ClaudeProvider(api_key=SECRET, model=model, client=client), client


def test_claude_sends_the_instructions_the_note_and_the_shape():
    provider, client = claude(reply())
    provider.complete(ModelRequest(system="Extract.", user="<note>Hi</note>", json_schema=SCHEMA))

    (sent,) = client.sent
    assert sent == {
        "model": "claude-opus-5-5",
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": "Extract.",
        "messages": [{"role": "user", "content": "<note>Hi</note>"}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def test_claude_is_given_no_tools():
    provider, client = claude(reply())
    provider.complete(REQUEST)
    assert "tools" not in client.sent[0] and "tool_choice" not in client.sent[0]


def test_claude_request_without_a_shape_sends_no_format():
    provider, client = claude(reply())
    provider.complete(REQUEST)
    assert client.sent[0]["output_config"] == {"effort": "low"}


def test_claude_returns_the_text_and_the_model_that_answered():
    provider, _ = claude(reply(text='{"account": null}', model="claude-opus-5"))
    response = provider.complete(REQUEST)
    assert response == ModelResponse(text='{"account": null}', provider="claude", model="claude-opus-5")


def test_claude_uses_the_model_it_was_given():
    provider, client = claude(reply(), model="claude-haiku-5-5")
    provider.complete(REQUEST)
    assert client.sent[0]["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize(
    "bad_reply, message",
    [
        (reply(stop_reason="refusal", blocks=[]), "declined"),
        (reply(stop_reason="max_tokens"), "cut off"),
        (reply(blocks=[SimpleNamespace(type="thinking", thinking="")]), "no text"),
        (reply(blocks=[]), "no text"),
    ],
)
def test_claude_reply_that_cannot_be_used_is_an_error(bad_reply, message):
    provider, _ = claude(bad_reply)
    with pytest.raises(ModelError, match=message):
        provider.complete(REQUEST)


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return cls(f"server said: your key {SECRET} is bad", response=response, body=None)


_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "sdk_error, message",
    [
        (_status_error(anthropic.AuthenticationError, 401), "rejected the API key"),
        (_status_error(anthropic.PermissionDeniedError, 403), "not allowed"),
        (_status_error(anthropic.NotFoundError, 404), "does not know this model"),
        (_status_error(anthropic.RateLimitError, 429), "rate limit"),
        (_status_error(anthropic.BadRequestError, 400), "rejected the request"),
        (_status_error(anthropic.InternalServerError, 500), "status 500"),
        (anthropic.APIConnectionError(request=_REQUEST), "could not be reached"),
        (anthropic.APITimeoutError(request=_REQUEST), "could not be reached"),
    ],
)
def test_every_sdk_failure_becomes_one_plain_error_without_the_key(sdk_error, message):
    provider, _ = claude(sdk_error)
    with pytest.raises(ModelError, match=message) as error:
        provider.complete(REQUEST)
    assert SECRET not in str(error.value)
    assert "server said" not in str(error.value)


def test_claude_refuses_anything_that_is_not_a_request():
    provider, client = claude(reply())
    with pytest.raises(TypeError):
        provider.complete("Visited Acme today.")
    assert client.sent == []


@pytest.mark.parametrize("key", ["", "   ", None])
def test_claude_needs_a_key_before_anything_is_contacted(key):
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        ClaudeProvider(api_key=key)


def test_request_shape_must_be_a_dict():
    with pytest.raises(ValueError, match="json_schema"):
        ModelRequest(system="Extract.", user="Hi", json_schema="{}")


# --- Choosing Claude ---------------------------------------------------------------


def test_claude_is_chosen_by_name_and_reads_its_settings():
    provider = get_provider(
        {"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": SECRET, "LLM_MODEL": "claude-haiku-5-5"}
    )
    assert isinstance(provider, ClaudeProvider)
    assert provider._model == "claude-haiku-5-5"


def test_blank_model_setting_means_the_default_model():
    provider = get_provider({"LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": SECRET, "LLM_MODEL": ""})
    assert provider._model == DEFAULT_CLAUDE_MODEL


def test_choosing_claude_without_a_key_fails_clearly():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is not set"):
        get_provider({"LLM_PROVIDER": "claude"})
