"""Starts the web app with the settings from .env.

    .venv/bin/uvicorn --factory fieldnote.main:app

With LLM_PROVIDER=claude in .env, every submitted note makes a real, paid API
call and sends the note's text to Anthropic. Use invented notes only.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from fieldnote.api import create_app
from fieldnote.catalog import Catalog
from fieldnote.config import load_settings
from fieldnote.crm import crm_mode
from fieldnote.llm import get_provider

ROOT = Path(__file__).resolve().parents[2]  # the repository folder
DEFAULT_DATABASE = "fieldnote.db"


def build_app(settings: Mapping[str, str], root: Path) -> FastAPI:
    """Build the app from settings. root is the folder that holds data/."""
    # Refuse to start with a CRM mode we cannot run, rather than find out at
    # the first write. Nothing writes to the CRM yet.
    crm_mode(settings)
    return create_app(
        db_path=settings.get("DATABASE_PATH") or root / DEFAULT_DATABASE,
        provider=get_provider(settings),
        catalog=Catalog.from_csv(root / "data" / "catalog.csv"),
    )


def app() -> FastAPI:
    return build_app(load_settings(ROOT / ".env"), ROOT)
