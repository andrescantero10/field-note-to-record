"""Model adapter: the only module in the app that may talk to an AI model.

Everything else asks for a ModelProvider and calls complete() on it. A
provider receives text and returns text. It is given no tools, and nothing it
returns is ever executed: callers treat the response as untrusted data.

FakeProvider is the default, so the app and every test run with no API key.
A real provider is added to _PROVIDERS when it is built; tests/test_boundaries.py
fails if any other module imports a model SDK or an HTTP client.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

DEFAULT_PROVIDER = "fake"


class ModelError(Exception):
    """The provider could not produce a response (network, quota, bad reply)."""


@dataclass(frozen=True)
class ModelRequest:
    """What is sent to the model: the instructions (system) and the text to
    work on (user). Both are plain text."""

    system: str
    user: str

    def __post_init__(self) -> None:
        for name in ("system", "user"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")


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


# Provider name -> a function that builds the provider from the settings.
# The real provider is added here when it is built.
_PROVIDERS: dict[str, Callable[[Mapping[str, str]], ModelProvider]] = {
    "fake": lambda env: FakeProvider(),
}


def get_provider(env: Mapping[str, str]) -> ModelProvider:
    """Return the provider named by LLM_PROVIDER in env (pass os.environ).

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
