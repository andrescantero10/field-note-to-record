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
NETWORK = {"httpx", "httpx2", "requests", "aiohttp", "urllib", "urllib3", "http", "socket", "ssl"}
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
import httpx2

def late():
    import socket
    return __import__("requests")
"""
    assert imported_modules(source) == {
        "os", "httpx", "httpx2", "anthropic", "urllib", "socket", "importlib",
    }


def test_scan_covers_the_real_source_files():
    names = {path.relative_to(SRC).as_posix() for path in SOURCE_FILES}
    assert {"llm.py", "pricing.py", "policy.py", "audit.py", "catalog.py", "api.py", "main.py"} <= names


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_only_the_model_adapter_may_reach_a_model_or_the_network(path):
    relative = path.relative_to(SRC).as_posix()
    restricted_imports = imported_modules(path.read_text(encoding="utf-8")) & RESTRICTED
    not_allowed = restricted_imports - ALLOWED.get(relative, set())
    assert not not_allowed, f"{relative} imports {sorted(not_allowed)}"


def test_both_http_libraries_are_restricted():
    # The model SDK is built on httpx2, so it must be on the list as well.
    assert {"httpx", "httpx2"} <= RESTRICTED


# --- Who may use the CRM adapter ---------------------------------------------------
# Hard rule: only one module talks to Zoho, and only after the policy check.
# crm.py is that module. It checks no policy itself, so the modules allowed to
# import it are listed here. Task 11 adds the writer, which checks the policy
# before every call. The web API is not on the list and must never be.

CRM_USERS = {"main.py"}


def imported_own_modules(source: str) -> set[str]:
    """Names of this project's own modules that a piece of source imports,
    for example {"crm", "audit"}."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "fieldnote" and len(parts) > 1:
                    names.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0:  # from . import x / from .x import y
                parts = module.split(".") if module else []
            elif module == "fieldnote" or module.startswith("fieldnote."):
                parts = module.split(".")[1:]
            else:
                continue
            if parts:
                names.add(parts[0])
            else:
                names.update(alias.name for alias in node.names)
    return names


def test_own_module_scanner_sees_every_way_of_importing():
    source = """
import fieldnote.crm
import fieldnote.audit as log
from fieldnote.policy import PolicyEngine
from fieldnote import catalog, pricing
from . import llm
from .extraction import extract
import os
from somewhere.crm import Thing
"""
    assert imported_own_modules(source) == {
        "crm", "audit", "policy", "catalog", "pricing", "llm", "extraction",
    }


@pytest.mark.parametrize(
    "path", SOURCE_FILES, ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_only_listed_modules_may_use_the_crm_adapter(path):
    relative = path.relative_to(SRC).as_posix()
    uses_crm = "crm" in imported_own_modules(path.read_text(encoding="utf-8"))
    if relative == "crm.py":
        return
    assert not uses_crm or relative in CRM_USERS, f"{relative} imports the CRM adapter"


def test_the_crm_adapter_stands_alone():
    # It uses none of the project's other modules: no policy, no model, no
    # prices, no candidates. And until the live client is built, no network.
    source = (SRC / "crm.py").read_text(encoding="utf-8")
    assert imported_own_modules(source) == set()
    assert imported_modules(source) & RESTRICTED == set()
    assert "api.py" not in CRM_USERS
