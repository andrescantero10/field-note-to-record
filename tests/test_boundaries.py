"""Architecture boundaries, checked by reading the source code.

Hard rule: only one module talks to a model. This test reads the imports of
every file in src/fieldnote/ and fails if a file imports a model SDK or a
network library it is not allowed to. It reads the code as text (with Python's
ast module) and never runs it.

Later tasks extend ALLOWED: the Zoho module will be allowed an HTTP client,
and the validation module will be forbidden from importing pricing or catalog.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "fieldnote"
SOURCE_FILES = sorted(SRC.rglob("*.py"))

MODEL_SDKS = {"anthropic", "openai", "google", "cohere", "mistralai", "boto3"}
NETWORK = {"httpx", "requests", "aiohttp", "urllib", "urllib3", "http", "socket", "ssl"}
# importlib and __import__ load a module by name at run time, which this scan
# cannot see through, so they are restricted too.
DYNAMIC_IMPORT = {"importlib"}
RESTRICTED = MODEL_SDKS | NETWORK | DYNAMIC_IMPORT

# File (relative to src/fieldnote/) -> restricted modules it may import.
ALLOWED = {
    "llm.py": MODEL_SDKS | NETWORK,
}


def imported_modules(source: str) -> set[str]:
    """Top-level names of every module a piece of source code imports."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            names.add("importlib")
    return names


def test_scanner_sees_every_way_of_importing():
    source = """
import os.path
import httpx as client
from anthropic import Anthropic
from urllib.request import urlopen
from . import sibling
from .audit import AuditLog

def late():
    import socket
    return __import__("requests")
"""
    assert imported_modules(source) == {
        "os", "httpx", "anthropic", "urllib", "socket", "importlib",
    }


def test_scan_covers_the_real_source_files():
    names = {path.relative_to(SRC).as_posix() for path in SOURCE_FILES}
    assert {"llm.py", "pricing.py", "policy.py", "audit.py", "catalog.py"} <= names


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_only_the_model_adapter_may_reach_a_model_or_the_network(path):
    relative = path.relative_to(SRC).as_posix()
    restricted_imports = imported_modules(path.read_text(encoding="utf-8")) & RESTRICTED
    not_allowed = restricted_imports - ALLOWED.get(relative, set())
    assert not not_allowed, f"{relative} imports {sorted(not_allowed)}"
