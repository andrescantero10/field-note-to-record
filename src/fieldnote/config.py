"""Settings: read from a .env file and from environment variables.

This is the one place the app looks at its environment. Everything else is
handed the settings it needs. Values are secrets (API keys): never print or
log what these functions return.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read KEY=value lines from a file. A missing file gives no settings.

    Blank lines, lines starting with # and lines without = are skipped.
    Quotes around a value are removed. Nothing is evaluated or expanded.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_settings(
    env_file: str | os.PathLike[str] = ".env",
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The settings from env_file, overridden by real environment variables.
    environ defaults to os.environ; tests pass their own."""
    return {**read_env_file(env_file), **(os.environ if environ is None else environ)}
