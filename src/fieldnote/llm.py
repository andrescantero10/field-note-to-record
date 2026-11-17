"""Model adapter: the only module in the app that may talk to an AI model.

Everything else asks for a ModelProvider and calls complete() on it. A
provider receives text and returns text. It is given no tools, and nothing it
returns is ever executed: callers treat the response as untrusted data.

Two providers exist:

- FakeProvider, the default: scripted replies, no network, no API key.
- ClaudeProvider: one request to the Claude API per call.

tests/test_boundaries.py fails if any other module imports a model SDK or an
HTTP client.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

DEFAULT_PROVIDER = "fake"

# --- Claude settings ---------------------------------------------------------
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
# Room for the answer plus the model's own reasoning. Hitting the cap cuts the
# answer off, which is reported as an error rather than half-read.
MAX_OUTPUT_TOKENS = 16_000
# How hard the model works on each request. Extraction is copying, not hard
# reasoning, so low is the starting point; raise it if quotes come back wrong.
EFFORT = "low"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 2  # the SDK retries rate limits, server errors and timeouts
# If Claude's safety filters decline a request, the API retries it once on the
# model Anthropic recommends, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelError(Exception):
    """The provider could not produce a response (network, quota, bad reply).
    The message is always written here; it never repeats the provider's own
    error text, which could contain request details."""


@dataclass(frozen=True)
class ModelRequest:
    """What is sent to the model: the instructions (system) and the text to
    work on (user). json_schema, when given, is the exact shape the answer
    must have; a real provider asks the API to enforce it."""

    system: str
    user: str
    json_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("system", "user"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
        if self.json_schema is not None and not isinstance(self.json_schema, dict):
            raise ValueError("json_schema must be a dict or None")


@dataclass(frozen=True)
class ModelResponse:
    """What came back. text is untrusted: it is data to be checked, never
    an instruction to follow."""

    text: str
    provider: str
    model: str


class ModelProvider(Protocol):
    """The shape every provider has: one method, text in and text out.
    Any class with a matching complete() method counts as a ModelProvider."""

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class FakeProvider:
    """A stand-in model for tests and for running the app offline.

    responder is either a fixed reply or a function that builds the reply from
    the request. Every request is kept in .calls, in order, so a test can check
    exactly what the app sent to the model.
    """

    def __init__(self, responder: str | Callable[[ModelRequest], str] = "{}") -> None:
        if not isinstance(responder, str) and not callable(responder):
            raise TypeError("responder must be a string or a function")
        self._responder = responder
        self.calls: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")
        # Record first, so a request is remembered even if the responder fails.
        self.calls.append(request)
        text = self._responder if isinstance(self._responder, str) else self._responder(request)
        if not isinstance(text, str):
            raise TypeError("the fake responder must return a string")
        return ModelResponse(text=text, provider="fake", model="fake")


class ClaudeProvider:
    """Sends one request to the Claude API and returns the text of the answer.

    client is only passed in by tests, to stand in for the real SDK client.
    """

    def __init__(self, api_key: str, model: str = DEFAULT_CLAUDE_MODEL, *, client: Any = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ANTHROPIC_API_KEY is not set; add it to your .env file")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("the model name must be a non-blank string")
        self._model = model
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")

        output_config: dict[str, Any] = {"effort": EFFORT}
        if request.json_schema is not None:
            # Structured outputs: the API guarantees the answer is JSON of this shape.
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}

        try:
            # No tools are passed: the model can only answer with text.
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config=output_config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        # Most specific first. Each message is our own wording.
        except anthropic.AuthenticationError as error:
            raise ModelError("the Claude API rejected the API key") from error
        except anthropic.PermissionDeniedError as error:
            raise ModelError("the API key is not allowed to use this model") from error
        except anthropic.NotFoundError as error:
            raise ModelError("the Claude API does not know this model name") from error
        except anthropic.RateLimitError as error:
            raise ModelError("the Claude API rate limit was reached") from error
        except anthropic.BadRequestError as error:
            raise ModelError("the Claude API rejected the request") from error
        except anthropic.APIStatusError as error:
            raise ModelError(f"the Claude API returned an error (status {error.status_code})") from error
        except anthropic.APIConnectionError as error:
            raise ModelError("the Claude API could not be reached") from error

        # A refusal and a cut-off answer both arrive as a normal response.
        if response.stop_reason == "refusal":
            raise ModelError("Claude declined the request")
        if response.stop_reason == "max_tokens":
            raise ModelError("Claude's answer was cut off before it finished")
        for block in response.content:
            if block.type == "text":
                # response.model is the model that actually answered (it can
                # differ from the one asked for if a fallback ran).
                return ModelResponse(text=block.text, provider="claude", model=response.model)
        raise ModelError("Claude's answer contained no text")


def _build_claude(env: Mapping[str, str]) -> ModelProvider:
    return ClaudeProvider(
        api_key=env.get("ANTHROPIC_API_KEY", ""),
        model=env.get("LLM_MODEL") or DEFAULT_CLAUDE_MODEL,
    )


# Provider name -> a function that builds the provider from the settings.
_PROVIDERS: dict[str, Callable[[Mapping[str, str]], ModelProvider]] = {
    "fake": lambda env: FakeProvider(),
    "claude": _build_claude,
}


def get_provider(env: Mapping[str, str]) -> ModelProvider:
    """Return the provider named by LLM_PROVIDER in env (pass the settings from
    fieldnote.config.load_settings()).

    A missing setting means the fake. Any other value must match a known
    provider name exactly; there is no fallback, so a typo cannot silently
    switch providers.
    """
    name = env.get("LLM_PROVIDER", DEFAULT_PROVIDER)
    builder = _PROVIDERS.get(name)
    if builder is None:
        # Name only the setting that is wrong. Never echo env: it holds keys.
        raise ValueError(f"unknown LLM_PROVIDER {name!r}; known providers: {sorted(_PROVIDERS)}")
    return builder(env)
