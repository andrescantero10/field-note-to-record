"""Smoke test: the package is installed and importable.

This fails if `pip install -e ".[dev]"` was not run, if the src/ layout is
broken, or if the installed metadata and the source disagree on the version.
"""

from importlib.metadata import version

import fieldnote


def test_installed_version_matches_source():
    assert version("fieldnote") == fieldnote.__version__
