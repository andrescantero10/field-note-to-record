"""Tests for the phone page: how it is served, the product list it uses, and
its source code.

The page runs in a browser, which these tests cannot do. So the rule "text
from a note is shown as text, never run" is checked in two ways here: by
reading the page's source for anything that could turn text into code, and by
checking that the server tells the browser to run only our own script file.
Opening the page in a real browser with the hostile sample notes is a manual
check (see docs/AI_ASSISTANCE.md).
"""

import csv
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fieldnote.api import PAGE_FILES, STATIC, create_app
from fieldnote.catalog import Catalog
from fieldnote.llm import FakeProvider

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.from_csv(ROOT / "data" / "catalog.csv")
REP = {"X-Acting-As": "sales_rep"}

HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")
STYLE = (STATIC / "app.css").read_text(encoding="utf-8")
SOURCES = {"index.html": HTML, "app.js": SCRIPT, "app.css": STYLE}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "app.db", provider=FakeProvider(), catalog=CATALOG))


# --- Serving the page ------------------------------------------------------------


def test_the_page_is_exactly_three_files():
    assert PAGE_FILES == {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
    assert sorted(p.name for p in STATIC.iterdir()) == ["app.css", "app.js", "index.html"]


@pytest.mark.parametrize(
    "path, content_type, source",
    [("/", "text/html", HTML), ("/app.js", "text/javascript", SCRIPT), ("/app.css", "text/css", STYLE)],
)
def test_page_files_are_served_without_naming_a_user(client, path, content_type, source):
    response = client.get(path)  # no X-Acting-As: the page itself is not data

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.text == source
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_the_browser_is_told_to_run_only_our_own_files(client):
    policy = client.get("/").headers["content-security-policy"]

    assert "default-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "*" not in policy and "http" not in policy


def test_no_other_file_can_be_fetched(client):
    for path in ("/index.html", "/static/app.js", "/app.py", "/..%2fapi.py", "/app.js/../api.py"):
        assert client.get(path).status_code == 404, path


# --- The product list ---------------------------------------------------------------


def test_products_are_ids_and_names_only(client):
    response = client.get("/products", headers=REP)

    assert response.status_code == 200
    products = response.json()["products"]
    assert products == [
        {"id": product_id, "name": name}
        for product_id, name in sorted(CATALOG.product_names().items())
    ]
    assert {"ARM-6KG", "GRIP-2F-50", "VIS-3D"} <= {p["id"] for p in products}


def test_the_product_list_contains_no_price(client):
    text = client.get("/products", headers=REP).text
    with open(ROOT / "data" / "catalog.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows
    for row in rows:
        whole = row["unit_price"].split(".")[0]
        assert row["unit_price"] not in text
        assert whole not in text and f"{int(whole):,}" not in text
    for word in ("price", "tax", "total", "discount"):
        assert word not in text.lower()


# --- The page's source code ------------------------------------------------------------

# Anything here can turn a piece of text into HTML or into running code.
TEXT_TO_CODE = [
    "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser",
    "createContextualFragment", "srcdoc", "eval(", "Function(", "javascript:",
    "setAttribute(\"on", "setAttribute('on",
]


@pytest.mark.parametrize("name", sorted(SOURCES))
@pytest.mark.parametrize("forbidden", TEXT_TO_CODE)
def test_the_page_never_turns_text_into_code(name, forbidden):
    assert forbidden not in SOURCES[name]


def test_timers_are_given_functions_not_text():
    for call in re.findall(r"set(?:Timeout|Interval)\(\s*(.)", SCRIPT):
        assert call not in "\"'`"


def test_the_page_shows_text_with_textcontent():
    assert "textContent" in SCRIPT
    assert '"use strict"' in SCRIPT


def test_the_html_has_no_inline_script_or_style():
    # Inline code would be blocked by the rule the server sends, and it should
    # not be there in the first place.
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", HTML, flags=re.DOTALL)
    assert scripts == [""]
    assert re.findall(r"<script\b[^>]*>", HTML) == ['<script src="app.js" defer>']
    assert "<style" not in HTML
    assert not re.search(r"\sstyle\s*=", HTML)
    assert not re.search(r"\son[a-z]+\s*=", HTML)


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_the_page_loads_nothing_from_another_site(name):
    assert not re.search(r"https?:", SOURCES[name]), name
    assert '"//' not in SOURCES[name] and "'//" not in SOURCES[name]
    assert "@import" not in SOURCES[name] and "url(" not in SOURCES[name]


def test_the_script_only_calls_routes_that_exist(client):
    declared = {route.path.split("{")[0] for route in client.app.routes}
    called = set(re.findall(r'"(/(?:notes|candidates|products)[^"]*)"', SCRIPT))
    assert called, "the script should call the API"
    for path in called:
        assert any(d.startswith(path) or path.startswith(d) for d in declared if d != "/"), path


def test_the_script_names_the_acting_user_on_every_request():
    # One place builds every request, and it sends the header.
    assert SCRIPT.count("fetch(") == 1
    assert '"X-Acting-As"' in SCRIPT


def test_the_page_stores_nothing_in_the_browser():
    for storage in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
        assert storage not in SCRIPT
